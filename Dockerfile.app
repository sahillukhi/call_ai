FROM python:3.10-slim-buster

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY postprocess.py .
COPY prompt.txt .
COPY mcp_calendar ./mcp_calendar
COPY mcp_gmail ./mcp_gmail

# Copy frontend build
COPY front/dist ./front/dist

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
