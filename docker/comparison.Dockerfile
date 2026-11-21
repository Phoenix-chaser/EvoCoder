FROM evocoder-realworld:py312-v1
RUN python -m pip install --no-cache-dir hypothesis==6.112.1 attrs==24.2.0 six==1.16.0 python-dateutil==2.9.0.post0 jsonschema==4.23.0 tzdata==2024.2 pretend==1.0.9 testfixtures==8.3.0 freezegun==1.5.1 && python -m pip check && python -m pip freeze > /opt/comparison-packages.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
