FROM ghcr.io/astral-sh/uv:debian-slim as base

ENV PATH=/root/.local/bin:$PATH
ENV RUN_SEED_SCRIPT=true
ENV UV_PYTHON=3.13

RUN apt-get update && apt-get -y install --no-install-recommends \
	libdmtx-dev \
	libgl1 \
	libglib2.0-0 \
	build-essential \
	gcc \
	ca-certificates \
	curl \
	wget \
	v4l-utils \
	&& rm -rf /var/lib/apt/lists/* \
	&& apt-get clean
	
WORKDIR /app/perpetual_hardware/packages/perpetual_os1_server

RUN mkdir -p /app/perpetual_hardware/packages/perpetual_os1_server/data && \
	chmod -R 777 /app/perpetual_hardware/packages/perpetual_os1_server/data && \
	echo 'export "PATH=$PATH:my_path"' >> /etc/profile

EXPOSE 8000
EXPOSE 4222
EXPOSE 8222

CMD ["uv", "run", "--no-dev", "start"]
