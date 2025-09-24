FROM ghcr.io/astral-sh/uv:debian-slim
RUN apt-get update && apt-get -y install libdmtx-dev libgl1 libglib2.0-0
ADD . /app
WORKDIR /app/perpetual_hardware
ENV PATH=/root/.local/bin:$PATH
RUN echo 'export "PATH=$PATH:my_path"' >> /etc/profile
RUN uv sync --frozen
WORKDIR /app/perpetual_hardware/packages/perpetual_os1_server
RUN uv sync 
EXPOSE 8000
EXPOSE 4222
EXPOSE 8222
ENV RUN_SEED_SCRIPT=true
CMD ["uv", "run", "start"]

