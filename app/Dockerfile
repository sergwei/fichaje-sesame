FROM python:3.13.16-alpine3.24
COPY requirements.txt /app/requirements.txt
# Se quita pip después: sus librerías internas (urllib3, msgpack, setuptools) dan avisos en Trivy y no hacen falta.
RUN pip install --no-cache-dir --root-user-action=ignore -r /app/requirements.txt \
 && pip uninstall -y --root-user-action=ignore pip setuptools wheel
COPY fichaje.py /app/fichaje.py
ENV PYTHONUNBUFFERED=1
USER 1000:1000
CMD ["python", "-I", "/app/fichaje.py"]
