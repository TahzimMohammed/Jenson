# ns-3 environment for the edge-offloading project. Everyone builds from this file,
# so we all run the same ns-3 version, and the cluster (Apptainer) runs exactly the same thing.
#
#   docker build -t ns3-offload .                         (first build: 15-30 min)
#   docker build --build-arg JOBS=1 -t ns3-offload .      (if it stalls: low RAM)
#
# Layers are ordered so that changing the scenario or recommender only rebuilds the
# last few steps (about a minute); the big ns-3 build is cached.
FROM ubuntu:24.04
RUN printf 'Acquire::http::No-Cache "true";\nAcquire::http::Pipeline-Depth "0";\nAcquire::BrokenProxy "true";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/99network-fix
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ python3 cmake ninja-build git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG NS3_VERSION=ns-3.49
# Only the modules offload-edge.cc uses (ns-3 adds their dependencies). Much faster to build.
ARG NS3_MODULES="core;network;internet;point-to-point;applications"
ARG JOBS=2
RUN git clone --depth 1 --branch ${NS3_VERSION} https://gitlab.com/nsnam/ns-3-dev.git /opt/ns-3

WORKDIR /opt/ns-3
# optimized = fast simulations (no debug logging or asserts); fine for experiments.
RUN ./ns3 configure --build-profile=optimized --disable-examples --disable-tests \
        --enable-modules "${NS3_MODULES}" \
    && ./ns3 build -j${JOBS}

# Charts for experiments.py. Added after the ns-3 build so the slow layers stay cached.
RUN apt-get update && apt-get install -y --no-install-recommends python3-matplotlib \
    && rm -rf /var/lib/apt/lists/*

# ---- project layer (rebuilds quickly when our code changes) ----
# Compile the scenario into the image, so it runs in a read-only Apptainer image on the
# cluster without compiling at run time (run-one.sh uses /usr/local/bin/offload-sim).
COPY scratch/ /opt/ns-3/scratch/
RUN ./ns3 build -j${JOBS} offload-edge \
    && ln -sf "$(ls /opt/ns-3/build/scratch/ns3*-offload-edge-optimized)" /usr/local/bin/offload-sim

COPY recommender.py run-one.sh /opt/offload/
RUN chmod +x /opt/offload/run-one.sh

ENV NS3_DIR=/opt/ns-3 \
    PATH="/opt/offload:${PATH}"
WORKDIR /work
