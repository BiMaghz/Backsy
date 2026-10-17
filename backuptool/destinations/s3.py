import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
from pathlib import Path
from backuptool.core.config import S3Config
from .base import BaseDestination, SendResult
from backuptool.utils.helpers import retry

class S3Destination(BaseDestination):
    config_model = S3Config

    def __init__(self, config: S3Config | dict, killer=None):
        super().__init__('S3', config, killer)
        self.endpoint_url = self.config.endpoint_url
        self.access_key = self.config.access_key
        self.secret_key = self.config.secret_key
        self.bucket_name = self.config.bucket_name
        self.region_name = self.config.region_name
        self.generate_link = self.config.generate_link
        self.link_expiration = 86400

        s3_config = Config(
            signature_version='s3v4',
            retries={'max_attempts': 3}
        )

        self.s3_client = boto3.client(
            's3',
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region_name,
            config=s3_config
        )

    @property
    def produces_link(self) -> bool:
        return self.generate_link

    def get_presigned_url(self, filename: str) -> str | None:
        if not self.generate_link:
            return None
        
        try:
            url = self.s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.bucket_name, 'Key': filename},
                ExpiresIn=self.link_expiration
            )
            return url
        except ClientError as e:
            self.logger.error(f"Failed to generate pre-signed URL: {e}")
            return None

    @retry(max_retries=3, delay=5, backoff=2)
    def _upload(self, archive_path: Path, object_name: str) -> None:
        self.s3_client.upload_file(str(archive_path), self.bucket_name, object_name)

    def _send(self, archive_path: Path, base_caption: str, link_caption: str = "") -> SendResult:
        if self.killer and self.killer.kill_now:
            self.logger.warning("S3 upload aborted by signal.")
            return SendResult(False, detail="interrupted by signal")
        self.logger.info(f"Processing backup for {self.name}...")
        file_size_mb = archive_path.stat().st_size / (1024 * 1024)
        object_name = archive_path.name

        self.logger.info(f"Uploading '{object_name}' ({file_size_mb:.2f} MB) to bucket '{self.bucket_name}'...")
        self._upload(archive_path, object_name)
        self.logger.info(f"Successfully uploaded to S3: {self.bucket_name}/{object_name}")

        # Only after the upload succeeded, so a failed upload never leaves a dead link in a caption.
        url = self.get_presigned_url(object_name)
        if url:
            self.logger.info("Generated S3 pre-signed URL for captions.")
            return SendResult(True, link=f"\n• [S3 Direct Link]({url})")
        return SendResult(True)
