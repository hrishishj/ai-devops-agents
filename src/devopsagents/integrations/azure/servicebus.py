"""Service Bus queue adapter. Messages carry only a job id; the database is the source of truth,
so a lost/duplicated message can never lose or double-run work (leases are DB-enforced)."""

from __future__ import annotations


class ServiceBusQueue:  # pragma: no cover - needs live Azure
    def __init__(self, namespace_fqdn: str) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.servicebus import ServiceBusClient

        self._client = ServiceBusClient(namespace_fqdn, DefaultAzureCredential())

    def send(self, queue: str, job_id: str, session_id: str) -> None:
        from azure.servicebus import ServiceBusMessage

        with self._client.get_queue_sender(queue) as sender:
            sender.send_messages(ServiceBusMessage(job_id, message_id=job_id))

    def receive(self, queue: str, max_messages: int = 1) -> list[str]:
        ids: list[str] = []
        with self._client.get_queue_receiver(queue, max_wait_time=2) as receiver:
            for msg in receiver.receive_messages(max_message_count=max_messages, max_wait_time=2):
                ids.append(str(msg))
                receiver.complete_message(msg)  # at-most-once; DB sweep recovers losses
        return ids
