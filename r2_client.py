import mimetypes

import boto3

import settings

PRESIGNED_URL_EXPIRES_IN = 3600


def _get_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.get('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.get("R2_ACCESS_KEY"),
        aws_secret_access_key=settings.get("R2_SECRET_KEY"),
        region_name="auto",
    )


def upload_file(local_path, key):
    content_type = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    _get_client().upload_file(
        local_path, settings.get("R2_BUCKET"), key, ExtraArgs={"ContentType": content_type}
    )
    return key


def upload_bytes(data, key, content_type="application/octet-stream"):
    _get_client().put_object(Bucket=settings.get("R2_BUCKET"), Key=key, Body=data, ContentType=content_type)
    return key


def presigned_url(key, expires_in=PRESIGNED_URL_EXPIRES_IN):
    return _get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.get("R2_BUCKET"), "Key": key},
        ExpiresIn=expires_in,
    )


def delete_prefix(prefix):
    client = _get_client()
    bucket = settings.get("R2_BUCKET")
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
