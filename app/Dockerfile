FROM python:3.11-alpine
WORKDIR /app
RUN apk add --no-cache gcc libpq-dev musl-dev
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5000
CMD ["python", "app.py"]
CMD ["sh", "-c", "python create_admin.py && exec gunicorn --bind 0.0.0.0:5000 --workers 2 app:app"]