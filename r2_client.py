import mimetypes

import boto3

from config import R2_ACCESS_KEY, R2_ACCOUNT_ID, R2_BUCKET, R2_PUBLIC_DOMAIN, R2_SECRET_KEY

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name="auto",
        )
    return _client


def _public_url(key):
    domain = R2_PUBLIC_DOMAIN.rstrip("/")
    return f"{domain}/{key}"


def upload_file(local_path, key):
    content_type = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    _get_client().upload_file(
        local_path, R2_BUCKET, key, ExtraArgs={"ContentType": content_type}
    )
    return _public_url(key)


def upload_bytes(data, key, content_type="application/octet-stream"):
    _get_client().put_object(Bucket=R2_BUCKET, Key=key, Body=data, ContentType=content_type)
    return _public_url(key)


def delete_prefix(prefix):
    client = _get_client()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=R2_BUCKET, Delete={"Objects": objects})
