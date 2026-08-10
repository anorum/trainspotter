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
# The base image is Ubuntu jammy, whose python3 is 3.10; blockade-core needs
# 3.11 (datetime.UTC among other things) and PyFlink 1.20 supports it. The
# deadsnakes PPA ships prebuilt 3.11 debs for arm64, so nothing here compiles
# under QEMU except pemja itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common gnupg \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3.11-dev gcc g++ openjdk-17-jdk-headless \
    && rm -rf /var/lib/apt/lists/*
# pemja's setup.py finds jni.h through JAVA_HOME, which must be the JDK here,
# not the runtime JRE. The path is architecture-suffixed on Debian/Ubuntu.
RUN export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture) \
    && python3.11 -m venv /opt/pyflink-env \
    && /opt/pyflink-env/bin/pip install --no-cache-dir apache-flink==1.20.3

COPY libs/blockade-core /tmp/blockade-core
RUN /opt/pyflink-env/bin/pip install --no-cache-dir /tmp/blockade-core


FROM flink:1.20.3-java17
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common gnupg \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get install -y --no-install-recommends python3.11 libpython3.11 \
    && apt-get purge -y software-properties-common gnupg && apt-get autoremove -y \
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
