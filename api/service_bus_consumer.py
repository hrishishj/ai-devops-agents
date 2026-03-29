"""
Azure Service Bus Consumer
==========================
Listens to three dedicated queues and dispatches messages to the
corresponding agents. Runs as a long-lived async process.

Queue mapping:
  coding-copilot-queue  → CodingCopilotAgent
  it-ops-queue          → ItOpsAgent
  test-security-queue   → TestSecurityAgent
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from typing import Any

from azure.servicebus.aio import ServiceBusClient, ServiceBusReceiver
from azure.servicebus import ServiceBusMessage, ServiceBusReceivedMessage
from azure.identity.aio import DefaultAzureCredential

from agents.coding_copilot.agent import CodingCopilotAgent
from agents.it_ops.agent import ItOpsAgent
from agents.test_security.agent import TestSecurityAgent
from shared.config import get_settings
from shared.telemetry import get_tracer, init_telemetry

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)
_settings = get_settings()

_SHUTDOWN = asyncio.Event()

# --------------------------------------------------------------------------- #
# Message handlers
# --------------------------------------------------------------------------- #

def _handle_coding(payload: dict[str, Any]) -> None:
    agent = CodingCopilotAgent()
    agent.__post_init__()
    agent.handle_pr(
        repo_full_name=payload["repo_full_name"],
        pr_number=int(payload["pr_number"]),
    )


def _handle_itops(payload: dict[str, Any]) -> None:
    agent = ItOpsAgent()
    agent.__post_init__()
    agent.handle_alert(
        alert_id=payload["alert_id"],
        source=payload.get("source", "service_bus"),
        log_payload=payload["log_payload"],
        metadata=payload.get("metadata"),
    )


def _handle_security(payload: dict[str, Any]) -> None:
    agent = TestSecurityAgent()
    agent.__post_init__()
    agent.handle_pr(
        repo_full_name=payload["repo_full_name"],
        pr_number=int(payload["pr_number"]),
    )


_QUEUE_HANDLERS = {
    _settings.servicebus_queue_coding: _handle_coding,
    _settings.servicebus_queue_itops: _handle_itops,
    _settings.servicebus_queue_security: _handle_security,
}

# --------------------------------------------------------------------------- #
# Consumer loop
# --------------------------------------------------------------------------- #

async def _consume_queue(
    client: ServiceBusClient,
    queue_name: str,
    handler,
) -> None:
    """Continuously receive messages from a single queue."""
    async with client.get_queue_receiver(
        queue_name=queue_name,
        max_wait_time=5,
    ) as receiver:
        logger.info("Listening on queue: %s", queue_name)
        while not _SHUTDOWN.is_set():
            try:
                messages = await receiver.receive_messages(
                    max_message_count=5,
                    max_wait_time=5,
                )
                for msg in messages:
                    await _process_message(receiver, msg, handler, queue_name)
            except Exception as exc:
                logger.error("Queue '%s' receive error: %s", queue_name, exc)
                await asyncio.sleep(5)


async def _process_message(
    receiver: ServiceBusReceiver,
    msg: ServiceBusReceivedMessage,
    handler,
    queue_name: str,
) -> None:
    body_str = str(msg)
    with tracer.start_as_current_span("servicebus.process") as span:
        span.set_attribute("servicebus.queue", queue_name)
        span.set_attribute("servicebus.message_id", str(msg.message_id))
        try:
            payload = json.loads(body_str)
            await asyncio.get_event_loop().run_in_executor(None, handler, payload)
            await receiver.complete_message(msg)
            logger.info("Message %s completed on queue %s.", msg.message_id, queue_name)
        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON in message %s: %s", msg.message_id, exc)
            await receiver.dead_letter_message(msg, reason="InvalidJSON")
        except KeyError as exc:
            logger.error("Missing field in message %s: %s", msg.message_id, exc)
            await receiver.dead_letter_message(msg, reason=f"MissingField:{exc}")
        except Exception as exc:
            logger.error("Handler failed for message %s: %s", msg.message_id, exc, exc_info=True)
            # Abandon → message returns to queue for retry
            await receiver.abandon_message(msg)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

async def main() -> None:
    init_telemetry()

    def _shutdown_handler(*_):
        logger.info("Shutdown signal received.")
        _SHUTDOWN.set()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    credential = DefaultAzureCredential()
    namespace = _settings.servicebus_connection_string

    # If connection string provided, use it directly; otherwise use Managed Identity
    if namespace.startswith("Endpoint=sb://"):
        async with ServiceBusClient.from_connection_string(namespace) as client:
            await _run_consumers(client)
    else:
        async with ServiceBusClient(namespace, credential) as client:
            await _run_consumers(client)


async def _run_consumers(client: ServiceBusClient) -> None:
    tasks = [
        asyncio.create_task(
            _consume_queue(client, queue_name, handler)
        )
        for queue_name, handler in _QUEUE_HANDLERS.items()
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
