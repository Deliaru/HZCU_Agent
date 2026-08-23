"""Registered-source ingestion and temporal campus memory."""

from hzcu_agent.ingestion.catalog import SourceRegistry
from hzcu_agent.ingestion.service import IngestionService

__all__ = ["IngestionService", "SourceRegistry"]
