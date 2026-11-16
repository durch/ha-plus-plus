ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN pip install --no-cache-dir "aiohttp>=3.9" "pyyaml>=6"

WORKDIR /app
COPY ha_knob /app/ha_knob
COPY knob.yaml /app/knob.default.yaml
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
