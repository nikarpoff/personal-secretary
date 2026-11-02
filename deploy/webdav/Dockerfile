FROM python:3.12-slim

# Separate WebDAV process, no assistant code, credentials, or state in this image.
RUN pip install --no-cache-dir \
    "WsgiDAV==4.4.0" "cheroot==11.1.2" "PyYAML==6.0.3" \
    && mkdir -p /var/lib/dav

COPY server.py /opt/webdav/server.py
EXPOSE 8080
ENTRYPOINT ["python", "/opt/webdav/server.py"]
