# Dockerfile for LangGraph Memory Filter PostgreSQL Database
# This is a customized PostgreSQL image with LangGraph checkpoint tables pre-initialized

FROM postgres:16-alpine

# Set environment variables (override these in docker-compose or runtime)
ENV POSTGRES_DB=langgraph_memory \
    POSTGRES_USER=langgraph \
    POSTGRES_PASSWORD=langgraph_password_change_me

# Install additional tools for debugging (optional)
RUN apk add --no-cache postgresql-contrib

# Health check
HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB} || exit 1

# Expose PostgreSQL port
EXPOSE 5432

# Use default postgres entrypoint
# Tables are created automatically by the filter on first run via PostgresSaver.setup()
