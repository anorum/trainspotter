# The Flink job image: Flink runtime + PyFlink + blockade-core + job.py.
#
# Two stages because of one awkward fact: pemja (PyFlink's JNI bridge) ships no
# arm64 Linux wheel, so it compiles from source - and that needs gcc plus JDK
# headers, which the JRE-only Flink runtime image does not carry. The builder
# stage has the toolchain; the final image gets the finished venv and nothing
# else. Base image and jar are pinned; all three artifacts were verified to
# exist for arm64 before being written down here.

FROM flink:1.20.3-java17 AS builder
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev gcc g++ openjdk-17-jdk-headless \
    && rm -rf /var/lib/apt/lists/*
# pemja's setup.py finds jni.h through JAVA_HOME, which must be the JDK here,
# not the runtime JRE. The path is architecture-suffixed on Debian/Ubuntu.
RUN export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture) \
    && python3 -m venv /opt/pyflink-env \
    && /opt/pyflink-env/bin/pip install --no-cache-dir apache-flink==1.20.3

COPY libs/blockade-core /tmp/blockade-core
RUN /opt/pyflink-env/bin/pip install --no-cache-dir /tmp/blockade-core


FROM flink:1.20.3-java17
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/pyflink-env /opt/pyflink-env

# The Kafka connector uber-jar serves both the DataStream source and the SQL
# sinks. In /opt/flink/lib so it is on every classpath.
ADD --chown=flink:flink \
    https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.4.0-1.20/flink-sql-connector-kafka-3.4.0-1.20.jar \
    /opt/flink/lib/

# S3 for checkpoints. Flink filesystems load as plugins - isolated classloaders
# in a plugins subdirectory - not from lib/.
RUN mkdir -p /opt/flink/plugins/s3-fs-hadoop \
    && cp /opt/flink/opt/flink-s3-fs-hadoop-*.jar /opt/flink/plugins/s3-fs-hadoop/

COPY services/pipeline/src/pipeline/job.py /opt/flink/usrlib/job.py
USER flink
