FROM langchain/langgraph-api:3.11







RUN PYTHONDONTWRITEBYTECODE=1 uv pip install --system --no-cache-dir -c /api/constraints.txt langgraph langchain langchain-openai langchain-core requests python-dotenv web3==6.15.0 eth-account==0.10.0



# -- Installing local requirements --
ADD requirements.txt /deps/outer-travel-defi-agent/src/requirements.txt

RUN PYTHONDONTWRITEBYTECODE=1 uv pip install --system --no-cache-dir -c /api/constraints.txt -r /deps/outer-travel-defi-agent/src/requirements.txt
# -- End of local requirements install --



# -- Adding non-package dependency travel-defi-agent --
ADD . /deps/outer-travel-defi-agent/src
RUN set -ex && \
    for line in '[project]' \
                'name = "travel-defi-agent"' \
                'version = "0.1"' \
                '[tool.setuptools.package-data]' \
                '"*" = ["**/*"]' \
                '[build-system]' \
                'requires = ["setuptools>=61"]' \
                'build-backend = "setuptools.build_meta"'; do \
        echo "$line" >> /deps/outer-travel-defi-agent/pyproject.toml; \
    done
# -- End of non-package dependency travel-defi-agent --



# -- Installing all local dependencies --

RUN for dep in /deps/*; do             echo "Installing $dep";             if [ -d "$dep" ]; then                 echo "Installing $dep";                 (cd "$dep" && PYTHONDONTWRITEBYTECODE=1 uv pip install --system --no-cache-dir -c /api/constraints.txt -e .);             fi;         done

# -- End of local dependencies install --

ENV LANGSERVE_GRAPHS='{"agent": "/deps/outer-travel-defi-agent/src/agent.py:workflow_app"}'







# -- Ensure user deps didn't inadvertently overwrite langgraph-api
RUN mkdir -p /api/langgraph_api /api/langgraph_runtime /api/langgraph_license && touch /api/langgraph_api/__init__.py /api/langgraph_runtime/__init__.py /api/langgraph_license/__init__.py
RUN PYTHONDONTWRITEBYTECODE=1 uv pip install --system --no-cache-dir --no-deps -e /api
# -- End of ensuring user deps didn't inadvertently overwrite langgraph-api --
# -- Removing build deps from the final image ~<:===~~~ --
RUN pip uninstall -y pip setuptools wheel
RUN rm -rf /usr/local/lib/python*/site-packages/pip* /usr/local/lib/python*/site-packages/setuptools* /usr/local/lib/python*/site-packages/wheel* && find /usr/local/bin -name "pip*" -delete || true
RUN rm -rf /usr/lib/python*/site-packages/pip* /usr/lib/python*/site-packages/setuptools* /usr/lib/python*/site-packages/wheel* && find /usr/bin -name "pip*" -delete || true
RUN uv pip uninstall --system pip setuptools wheel && rm /usr/bin/uv /usr/bin/uvx



WORKDIR /deps/outer-travel-defi-agent/src

# Start the LangGraph API server
CMD ["uvicorn", "langgraph_api.server:app", "--host", "0.0.0.0", "--port", "8000"]