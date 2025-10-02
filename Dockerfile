FROM ghcr.io/astral-sh/uv:debian-slim
RUN apt-get update && apt-get -y install libdmtx-dev libgl1 libglib2.0-0
ADD ./perpetual_hardware /app/perpetual_hardware
WORKDIR /app/perpetual_hardware/packages/perpetual_os1_server
ENV PATH=/root/.local/bin:$PATH
RUN echo 'export "PATH=$PATH:my_path"' >> /etc/profile
RUN uv sync --frozen 
EXPOSE 8000
EXPOSE 4222
EXPOSE 8222
ENV RUN_SEED_SCRIPT=true
CMD python3 -c "import pymodbus; print('PyModbus version:', pymodbus.__version__)"
CMD ["uv", "run", "start"]

