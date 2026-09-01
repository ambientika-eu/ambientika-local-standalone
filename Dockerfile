FROM python:3.12-slim

# Der Geräteserver braucht keine Übersetzer und keine Systempakete — alle
# Abhängigkeiten sind reines Python. Das hält das Abbild klein und die
# Angriffsfläche gering.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY protokoll/ ./protokoll/
COPY server/ ./server/
COPY konfiguration.beispiel.yaml VERSION ./

# Datenbank, Signaturgeheimnis und Konfiguration liegen im Volume, nicht im
# Abbild. Sonst wären sie bei jedem Update weg.
VOLUME ["/daten"]
WORKDIR /app/server

# 4521 REST für die App, 11000 für die Lüftungsgeräte.
EXPOSE 4521 11000

ENTRYPOINT ["python3", "haupt.py"]
CMD ["--konfig", "/daten/konfiguration.yaml"]
