FROM ghcr.io/astral-sh/uv:debian-slim
RUN apt-get update && apt-get -y install \
	libdmtx-dev \
	libgl1 \
	libglib2.0-0 \
	build-essential \
	gcc \
	ca-certificates \
	cargo \
	rustc \
	&& rm -rf /var/lib/apt/lists/*
ADD ./perpetual_hardware /app/perpetual_hardware
WORKDIR /app/perpetual_hardware/packages/perpetual_os1_server
ENV PATH=/root/.local/bin:$PATH
RUN echo 'export "PATH=$PATH:my_path"' >> /etc/profile
RUN uv sync --frozen
RUN mkdir -p /app/perpetual_hardware/packages/perpetual_os1_server/data && \
	chmod -R 777 /app/perpetual_hardware/packages/perpetual_os1_server/data
EXPOSE 8000
EXPOSE 4222
EXPOSE 8222
ENV RUN_SEED_SCRIPT=true
CMD ["uv", "run", "start"]

