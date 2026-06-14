"""
infrastructure/service_bus.py
────────────────────────────────────────────────────────────────────────────
Azure Service Bus client for sending cache invalidation messages.

After the indexer re-indexes a document and uploads the new JSON to blob,
it sends a message to Service Bus. An Azure Function reads the message
and calls cache.invalidate_index() to clear the stale Redis entry.

Message format:
    {
        "redis_key": "index:2026:medical:1000016:retiree",
        "blob_path": "2026/1000016/medical_ppo_retiree.json",
        "reason":    "reindexed"
    }

Environment variables:
    AZURE_SERVICE_BUS_CONNECTION_STRING — Service Bus namespace connection string
    AZURE_SERVICE_BUS_QUEUE_NAME        — queue name (default: cache-invalidation)

Local dev fallback:
    When AZURE_SERVICE_BUS_CONNECTION_STRING is not set, logs the message
    and skips sending. Zero impact on local development.
"""

import os
from config import settings
import json
import logging

logger = logging.getLogger(__name__)

SERVICE_BUS_CONNECTION_STRING = settings.AZURE_SERVICE_BUS_CONNECTION_STRING
QUEUE_NAME = settings.AZURE_SERVICE_BUS_QUEUE_NAME


async def send_cache_invalidation(
    redis_key: str,
    blob_path: str,
    reason: str = "reindexed",
) -> bool:
    """
    Sends a cache invalidation message to the Service Bus queue.

    Parameters:
        redis_key — the Redis key to invalidate
        blob_path — the blob path of the new index file
        reason    — why invalidation is happening (for logging/audit)

    Returns True on success, False on failure.

    Local dev fallback:
        Logs the message and returns True without sending.
    """
    message_body = json.dumps(
        {
            "redis_key": redis_key,
            "blob_path": blob_path,
            "reason": reason,
        }
    )

    if not SERVICE_BUS_CONNECTION_STRING:
        logger.info(
            "[service_bus] Local dev — skipping send. Message: %s", message_body
        )
        return True

    try:
        from azure.servicebus import ServiceBusClient, ServiceBusMessage

        with ServiceBusClient.from_connection_string(
            SERVICE_BUS_CONNECTION_STRING
        ) as client:
            with client.get_queue_sender(QUEUE_NAME) as sender:
                sender.send_messages(ServiceBusMessage(message_body))

        logger.info("[service_bus] Sent cache invalidation: redis_key=%s", redis_key)
        return True

    except Exception as exc:
        logger.error("[service_bus] send_cache_invalidation failed: %s", exc)
        return False
