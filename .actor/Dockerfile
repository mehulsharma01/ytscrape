FROM apify/actor-python:3.12
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . "apify>=2.0"
COPY .actor/main.py ./actor_main.py
CMD ["python", "-u", "actor_main.py"]
