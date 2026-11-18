# ns-3 environment for the edge-offloading project.
# Everyone builds from this file so we all run the same ns-3 version.
#
#   docker build -t ns3-offload .            (first build: 20-40 min)
#   docker build --build-arg JOBS=1 -t ns3-offload .   (if it stalls: low RAM)
FROM ubuntu:24.04
RUN printf 'Acquire::http::No-Cache "true";\nAcquire::http::Pipeline-Depth "0";\nAcquire::BrokenProxy "true";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/99network-fix
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ python3 cmake ninja-build git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG NS3_VERSION=ns-3.48
ARG JOBS=2
RUN git clone --depth 1 --branch ${NS3_VERSION} https://gitlab.com/nsnam/ns-3-dev.git /opt/ns-3

WORKDIR /opt/ns-3
RUN ./ns3 configure --enable-examples \
    && ./ns3 build -j${JOBS}

# Charts for experiments.py. Added after the ns-3 build so the slow layers stay cached.
RUN apt-get update && apt-get install -y --no-install-recommends python3-matplotlib \
    && rm -rf /var/lib/apt/lists/*

ENV NS3_DIR=/opt/ns-3
WORKDIR /work
