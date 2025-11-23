import boto3
from botocore.exceptions import ClientError
from pathlib import Path
from .base import BaseDestination
from backuptool.utils.helpers import retry

class S3Destination(BaseDestination):
    
    def __init__(self, config: dict):
        super().__init__('S3', config)
        self.endpoint_url = self.config.get('endpoint_url')
        self.access_key = self.config.get('access_key')
        self.secret_key = self.config.get('secret_key')
        self.bucket_name = self.config.get('bucket_name')
        self.region_name = self.config.get('region_name') 
        
        self.s3_client = boto3.client(
            's3',
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region_name
        )

    @retry(max_retries=3, delay=5, backoff=2)
    def send(self, archive_path: Path, base_caption: str, cloudflare_info: str = "") -> bool:
        self.logger.info(f"Processing backup for {self.name}...")
        
        file_size_mb = archive_path.stat().st_size / (1024 * 1024)
        object_name = archive_path.name

        self.logger.info(f"Uploading '{object_name}' ({file_size_mb:.2f} MB) to bucket '{self.bucket_name}'...")

        try:
            self.s3_client.upload_file(
                str(archive_path),
                self.bucket_name,
                object_name
            )
            
            self.logger.info(f"Successfully uploaded to S3: {self.endpoint_url}/{self.bucket_name}/{object_name}")
            return True

        except ClientError as e:
            self.logger.error(f"S3 Client Error: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error during S3 upload: {e}")
            raise