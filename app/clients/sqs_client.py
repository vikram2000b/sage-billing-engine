"""AWS SQS client for publishing and consuming billing events."""

import json
import asyncio
from typing import Any, Callable, Awaitable, Optional

import boto3

from app.core.config import settings
from app.core.logging import logger, tracer


class SQSClient:
    """Async-friendly SQS client for publishing and consuming messages.

    Uses boto3 (sync) wrapped in asyncio.to_thread for non-blocking I/O.
    """

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        """Lazy-init the boto3 SQS client."""
        if self._client is None:
            self._client = boto3.client("sqs", region_name=settings.AWS_REGION)
        return self._client

    # ─── Publishing ───

    async def publish(
        self,
        queue_url: str,
        message: dict[str, Any],
        message_group_id: Optional[str] = None,
        deduplication_id: Optional[str] = None,
    ) -> str:
        """Publish a message to an SQS queue.

        Returns the SQS MessageId.
        """
        with tracer.start_as_current_span("sqs.publish", attributes={
            "sqs.queue_url": queue_url,
        }):
            params: dict[str, Any] = {
                "QueueUrl": queue_url,
                "MessageBody": json.dumps(message, default=str),
            }
            if message_group_id:
                params["MessageGroupId"] = message_group_id
            if deduplication_id:
                params["MessageDeduplicationId"] = deduplication_id

            response = await asyncio.to_thread(
                self._get_client().send_message, **params
            )
            message_id = response["MessageId"]
            logger.info(f"Published SQS message {message_id} to {queue_url}")
            return message_id

    # ─── Consuming ───

    async def consume_loop(
        self,
        queue_url: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        max_messages: int = 10,
        wait_time_seconds: int = 20,
        visibility_timeout: int = 60,
    ) -> None:
        """Long-poll an SQS queue and process messages with the given handler.

        This runs indefinitely. Call from an asyncio task.
        """
        logger.info(f"Starting SQS consumer for {queue_url}")
        client = self._get_client()

        while True:
            try:
                response = await asyncio.to_thread(
                    client.receive_message,
                    QueueUrl=queue_url,
                    MaxNumberOfMessages=max_messages,
                    WaitTimeSeconds=wait_time_seconds,
                    VisibilityTimeout=visibility_timeout,
                )

                messages = response.get("Messages", [])
                if not messages:
                    continue

                for msg in messages:
                    receipt_handle = msg["ReceiptHandle"]
                    try:
                        body = json.loads(msg["Body"])

                        # Handle SNS-wrapped messages (Stripe webhooks via SNS → SQS)
                        if "Message" in body and "TopicArn" in body:
                            body = json.loads(body["Message"])

                        await handler(body)

                        # Delete message on success
                        await asyncio.to_thread(
                            client.delete_message,
                            QueueUrl=queue_url,
                            ReceiptHandle=receipt_handle,
                        )
                    except Exception as e:
                        logger.error(
                            f"Error processing SQS message: {e}",
                            exc_info=True,
                            extra={"queue_url": queue_url, "message_id": msg.get("MessageId")},
                        )
                        # Message will become visible again after visibility_timeout

            except Exception as e:
                logger.error(f"Error polling SQS queue {queue_url}: {e}", exc_info=True)
                await asyncio.sleep(5)  # Back off on polling errors


# Singleton
sqs_client = SQSClient()
