FROM ghcr.io/astral-sh/uv:alpine
RUN apk update
RUN apk add --no-cache bash
RUN apk add curl make cmake libc-dev libffi-dev g++ python3-dev openssl-dev libdmtx libdmtx-libs
RUN apk add --no-cache python3~=3.12
# RUN curl https://sh.rustup.rs -sSf | bash -s -- -y
RUN apk add --no-cache rust cargo
# RUN echo 'source $HOME/.cargo/env' >> $HOME/.bashrc
RUN apk add maturin
ADD . /app
WORKDIR /app/perpetual_hardware
ENV PATH=/root/.local/bin:$PATH
RUN echo 'export "PATH=$PATH:my_path"' >> /etc/profile
RUN uv sync --frozen
WORKDIR /app/perpetual_hardware/packages/perpetual-p0c-server
EXPOSE 8000
EXPOSE 4222
EXPOSE 8222
CMD ["uv", "run", "start"]

