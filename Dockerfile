# Dockerfile to create a reproducible development environment

# 1. Base Image
# Start from an official Python 3.12 slim image to keep it lightweight.
FROM python:3.12-slim

# 2. Environment Variables
# Prevents Python from writing .pyc files and ensures output is sent
# straight to the terminal without being buffered.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. Install Poetry and add it to the PATH
# The PATH is updated so that the shell can find the `poetry` command.
ENV PATH="/root/.local/bin:${PATH}"
RUN pip install --no-cache-dir pipx
RUN pipx install poetry

# 4. Set the working directory inside the container
WORKDIR /app

# 5. Install project dependencies
# Copy only the necessary files first to leverage Docker's layer caching.
# If these files don't change, Docker won't reinstall dependencies on every build.
COPY pyproject.toml poetry.lock ./

# Install dependencies using the corrected pyproject.toml
RUN poetry install --with dev,docs --no-root

# 6. Copy the rest of the application code
COPY . .