FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        binutils \
        build-essential \
        clang \
        file \
        gdb \
        git \
        ltrace \
        make \
        python3 \
        python3-pip \
        strace \
        tar \
        vim-tiny \
        xxd \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

CMD ["sleep", "infinity"]
