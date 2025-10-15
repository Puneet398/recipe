import boto3
import os
from dotenv import load_dotenv


load_dotenv()

# Load credentials securely from environment
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.getenv('AWS_REGION', 'ap-south-1')
BUCKET_NAME = os.getenv('AWS_S3_BUCKET')

# Initialize S3 client
s3 = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION
)

def list_files(bucket):
    try:
        response = s3.list_objects_v2(Bucket=bucket)
        contents = response.get('Contents', [])

        if not contents:
            print("No files found in bucket.")
            return

        print(f"Files in bucket '{bucket}':")
        for obj in contents:
            print(f" - {obj['Key']} ({obj['LastModified'].strftime('%Y-%m-%d %H:%M:%S')})")

    except Exception as e:
        print(f"Error listing files: {e}")

if __name__ == "__main__":
    if not BUCKET_NAME:
        print("AWS_S3_BUCKET not set in environment.")
    else:
        list_files(BUCKET_NAME)