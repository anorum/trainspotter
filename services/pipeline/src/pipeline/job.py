"""The Flink job: observations in, sessions and alerts out.

Deliberately thin. Every decision - when a session opens, closes, qualifies;
when an alert fires - lives in blockade-core classes that are unit-tested and
diffed against the batch oracle. Flink contributes exactly three things the
plain classes cannot: durable keyed state, event-time timers, and recovery
from checkpoints. If a line here looks like business logic, it is in the wrong
file.

Topology:

    observations.v1 (Kafka source, event-time watermarks)
        -> key_by(crossing_id)
        -> CrossingFunction        (sessionizer + rising-edge alerter)
            -> sessions, keyed by session_id  -> crossing.sessions.v1
            -> alerts (side output), keyed by crossing_id -> crossing.alerts.v1

Sinks go through Table API DDL because PyFlink's DataStream KafkaSink cannot
set a message key from a field, and the compacted sessions topic requires
keys. The `raw` format writes the payload column as the message value
untouched, so the topic carries exactly the pydantic JSON - same contract as
every other topic in the system.
"""

from __future__ import annotations

import json
import os

from blockade.alerts import CrossingAlertState, RisingEdgeAlerter
from blockade.schemas import ObservationRecord
from blockade.stream_sessions import SessionizerState, StreamingSessionizer
from pyflink.common import Duration, Row, Types, WatermarkStrategy
from pyflink.common.serialization import SimpleStringSchema
from pyflink.common.watermark_strategy import TimestampAssigner
from pyflink.datastream import (
    KeyedProcessFunction,
    OutputTag,
    RuntimeContext,
    StreamExecutionEnvironment,
)
from pyflink.datastream.connectors.kafka import (
    KafkaOffsetResetStrategy,
    KafkaOffsetsInitializer,
    KafkaSource,
)
from pyflink.datastream.state import ValueStateDescriptor
from pyflink.table import StreamTableEnvironment

BOOTSTRAP = os.environ["BLOCKADE_KAFKA_BOOTSTRAP"]
OBSERVATIONS_TOPIC = os.environ.get("BLOCKADE_OBSERVATIONS_TOPIC", "crossing.observations.v1")
SESSIONS_TOPIC = os.environ.get("BLOCKADE_SESSIONS_TOPIC", "crossing.sessions.v1")
ALERTS_TOPIC = os.environ.get("BLOCKADE_ALERTS_TOPIC", "crossing.alerts.v1")

# Camera refresh is irregular but single-writer per camera, so out-of-orderness
# only comes from two cameras covering one crossing drifting apart. Two minutes
# covers the worst drift seen; it is also the alert branch's worst-case added
# latency of exactly zero - alerts react per element, not per watermark.
OUT_OF_ORDERNESS = Duration.of_minutes(2)
# A quiet partition (night, one camera down) must not stall the watermark for
# every crossing. After this long silent, a partition stops holding time back.
IDLENESS = Duration.of_minutes(5)

ALERTS = OutputTag("alerts", Types.ROW([Types.STRING(), Types.STRING()]))


class ObservationTimestamps(TimestampAssigner):
    def extract_timestamp(self, value: str, record_timestamp: int) -> int:
        captured = ObservationRecord.model_validate_json(value).captured_at
        return int(captured.timestamp() * 1000)


class CrossingFunction(KeyedProcessFunction):
    """Host for the two tested cores. State and timers belong to Flink;
    decisions belong to the cores."""

    def open(self, ctx: RuntimeContext) -> None:
        self.sessionizer = StreamingSessionizer()
        self.alerter = RisingEdgeAlerter()
        self.session_state = ctx.get_state(ValueStateDescriptor("session", Types.STRING()))
        self.alert_state = ctx.get_state(ValueStateDescriptor("alert", Types.STRING()))

    def process_element(self, value: str, ctx: KeyedProcessFunction.Context):
        obs = ObservationRecord.model_validate_json(value)

        # --- session branch
        raw = self.session_state.value()
        state = SessionizerState.from_json_dict(json.loads(raw)) if raw else None
        state, sessions, timer = self.sessionizer.observe(state, obs)
        self._save_session(state)
        if timer is not None:
            ctx.timer_service().register_event_time_timer(timer)
        for session in sessions:
            # Row, not a tuple: the declared output type is ROW, and the Table
            # bridge that feeds the sink calls Row-specific methods on it. A
            # tuple survives graph construction and dies in the worker.
            yield Row(session.session_id, session.model_dump_json())

        # --- alert branch: per-element, no timers, so an alert never waits on
        # a watermark. The alerter keeps its own dict keyed by crossing_id;
        # here that dict holds exactly one entry, restored from Flink state.
        raw = self.alert_state.value()
        if raw:
            restored = CrossingAlertState.from_json_dict(json.loads(raw))
            self.alerter.states[obs.crossing_id] = restored
        alert = self.alerter.observe(obs)
        self.alert_state.update(json.dumps(self.alerter.states[obs.crossing_id].to_json_dict()))
        if alert is not None:
            payload = json.dumps(
                {
                    "crossing_id": alert.crossing_id,
                    "started_at": alert.started_at.isoformat(),
                    "confidence": alert.confidence,
                    "reason": alert.reason,
                }
            )
            yield ALERTS, Row(alert.crossing_id, payload)

    def _save_session(self, state: SessionizerState | None) -> None:
        if state is None:
            self.session_state.clear()
        else:
            self.session_state.update(json.dumps(state.to_json_dict()))

    def on_timer(self, timestamp: int, ctx: KeyedProcessFunction.OnTimerContext):
        raw = self.session_state.value()
        state = SessionizerState.from_json_dict(json.loads(raw)) if raw else None
        state, sessions = self.sessionizer.on_timer(state, timestamp)
        self._save_session(state)
        for session in sessions:
            yield Row(session.session_id, session.model_dump_json())


def keyed_kafka_sink_ddl(table: str, topic: str) -> str:
    """A Kafka sink whose message key is the first column and whose value is
    the second, byte for byte. `raw` format, so no wrapping JSON object."""
    return f"""
        CREATE TABLE {table} (
            key STRING,
            payload STRING
        ) WITH (
            'connector' = 'kafka',
            'topic' = '{topic}',
            'properties.bootstrap.servers' = '{BOOTSTRAP}',
            'key.format' = 'raw',
            'key.fields' = 'key',
            'value.format' = 'raw',
            'value.fields-include' = 'EXCEPT_KEY'
        )
    """


def main() -> None:
    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(1)
    table_env = StreamTableEnvironment.create(env)

    source = (
        KafkaSource.builder()
        .set_bootstrap_servers(BOOTSTRAP)
        .set_topics(OBSERVATIONS_TOPIC)
        .set_group_id("blockade-pipeline")
        # Committed offsets when they exist; earliest on first deployment so
        # the retained history streams through, same policy as the detector.
        .set_starting_offsets(
            KafkaOffsetsInitializer.committed_offsets(KafkaOffsetResetStrategy.EARLIEST)
        )
        .set_value_only_deserializer(SimpleStringSchema())
        .build()
    )
    watermarks = (
        WatermarkStrategy.for_bounded_out_of_orderness(OUT_OF_ORDERNESS)
        .with_idleness(IDLENESS)
        .with_timestamp_assigner(ObservationTimestamps())
    )

    observations = env.from_source(source, watermarks, "observations")
    keyed = observations.key_by(
        lambda value: ObservationRecord.model_validate_json(value).crossing_id,
        key_type=Types.STRING(),
    )
    processed = keyed.process(
        CrossingFunction(), output_type=Types.ROW([Types.STRING(), Types.STRING()])
    )

    table_env.execute_sql(keyed_kafka_sink_ddl("sessions_sink", SESSIONS_TOPIC))
    table_env.execute_sql(keyed_kafka_sink_ddl("alerts_sink", ALERTS_TOPIC))

    statements = table_env.create_statement_set()
    statements.add_insert("sessions_sink", table_env.from_data_stream(processed))
    alerts_stream = processed.get_side_output(ALERTS)
    statements.add_insert("alerts_sink", table_env.from_data_stream(alerts_stream))
    statements.attach_as_datastream()

    env.execute("blockade-pipeline")


if __name__ == "__main__":
    main()
