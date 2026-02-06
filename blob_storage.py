"""
Vercel Blob Storage Helper
Provides file upload/download functionality using Vercel Blob storage.
"""

import os
import io
from typing import Optional, Tuple
from werkzeug.datastructures import FileStorage
from vercel_blob import put, head, delete
from PIL import Image

# Get Blob token from environment
BLOB_TOKEN = os.environ.get('BLOB_READ_WRITE_TOKEN', '')

def is_blob_configured() -> bool:
    """Check if Vercel Blob is configured"""
    return bool(BLOB_TOKEN)

def upload_file(file: FileStorage, path: str, content_type: Optional[str] = None) -> Tuple[str, int]:
    """
    Upload a file to Vercel Blob storage.

    Args:
        file: FileStorage object from Flask request
        path: Path/key for the file in blob storage (e.g., 'audio/filename.mp3')
        content_type: MIME type of the file (optional, will be detected)

    Returns:
        Tuple of (blob_url, file_size)

    Raises:
        Exception if upload fails
    """
    if not is_blob_configured():
        raise Exception("Vercel Blob storage not configured. Set BLOB_READ_WRITE_TOKEN environment variable.")

    try:
        # Read file content
        file_content = file.read()
        file_size = len(file_content)

        # Detect content type if not provided
        if not content_type:
            content_type = file.content_type or 'application/octet-stream'

        # Use multipart upload for files larger than 4MB
        use_multipart = file_size > 4 * 1024 * 1024

        # Upload to Vercel Blob (correct API usage)
        response = put(
            path,  # First positional argument
            file_content,  # Second positional argument (bytes)
            options={
                'access': 'public',
                'token': BLOB_TOKEN,
                'addRandomSuffix': False,
                'contentType': content_type
            },
            multipart=use_multipart,
            timeout=120  # Increase timeout for large files
        )

        blob_url = response['url']

        return blob_url, file_size

    except Exception as e:
        import traceback
        error_msg = f"Failed to upload file to Blob storage: {e}\n{traceback.format_exc()}"
        print(error_msg)
        raise Exception(error_msg)

def upload_bytes(data: bytes, path: str, content_type: str = 'application/octet-stream') -> str:
    """
    Upload raw bytes to Vercel Blob storage.

    Args:
        data: Raw bytes to upload
        path: Path/key for the file in blob storage
        content_type: MIME type of the data

    Returns:
        blob_url: URL to access the uploaded file

    Raises:
        Exception if upload fails
    """
    if not is_blob_configured():
        raise Exception("Vercel Blob storage not configured. Set BLOB_READ_WRITE_TOKEN environment variable.")

    try:
        response = put(
            path,  # First positional argument
            data,  # Second positional argument (bytes)
            options={
                'access': 'public',
                'token': BLOB_TOKEN,
                'addRandomSuffix': False,
                'contentType': content_type
            }
        )

        return response['url']

    except Exception as e:
        import traceback
        error_msg = f"Failed to upload bytes to Blob storage: {e}\n{traceback.format_exc()}"
        print(error_msg)
        raise Exception(error_msg)

def upload_thumbnail(image_path_or_file, thumbnail_path: str, size: Tuple[int, int] = (150, 150)) -> Optional[str]:
    """
    Create and upload a thumbnail to Vercel Blob storage.

    Args:
        image_path_or_file: Either a local file path or FileStorage object
        thumbnail_path: Path/key for the thumbnail in blob storage
        size: Thumbnail dimensions (width, height)

    Returns:
        blob_url of the thumbnail, or None if creation fails
    """
    if not is_blob_configured():
        return None

    try:
        # Open image
        if isinstance(image_path_or_file, str):
            img = Image.open(image_path_or_file)
        else:
            img = Image.open(image_path_or_file)

        # Create thumbnail
        img.thumbnail(size, Image.Resampling.LANCZOS)

        # Save to bytes buffer
        buffer = io.BytesIO()
        img_format = img.format or 'JPEG'
        img.save(buffer, format=img_format, optimize=True, quality=85)
        thumbnail_data = buffer.getvalue()

        # Upload thumbnail
        content_type = f"image/{img_format.lower()}"
        if img_format.upper() == 'JPEG':
            content_type = 'image/jpeg'
        elif img_format.upper() == 'PNG':
            content_type = 'image/png'

        blob_url = upload_bytes(thumbnail_data, thumbnail_path, content_type)

        return blob_url

    except Exception as e:
        print(f"Error creating/uploading thumbnail: {e}")
        return None

def delete_file(blob_url: str) -> bool:
    """
    Delete a file from Vercel Blob storage.

    Args:
        blob_url: Full URL of the blob to delete

    Returns:
        True if deleted successfully, False otherwise
    """
    if not is_blob_configured():
        return False

    try:
        delete(url=blob_url, options={'token': BLOB_TOKEN})
        return True
    except Exception as e:
        print(f"Error deleting blob: {e}")
        return False

def get_file_info(blob_url: str) -> Optional[dict]:
    """
    Get metadata about a file in Vercel Blob storage.

    Args:
        blob_url: Full URL of the blob

    Returns:
        Dictionary with file metadata, or None if error
    """
    if not is_blob_configured():
        return None

    try:
        info = head(url=blob_url, options={'token': BLOB_TOKEN})
        return {
            'url': info.get('url'),
            'size': info.get('size'),
            'uploadedAt': info.get('uploadedAt'),
            'contentType': info.get('contentType'),
            'contentDisposition': info.get('contentDisposition'),
        }
    except Exception as e:
        print(f"Error getting file info: {e}")
        return None

def generate_blob_path(category: str, filename: str) -> str:
    """
    Generate a standardized blob storage path.

    Args:
        category: Category/folder (e.g., 'audio', 'covers', 'documents', 'attachments')
        filename: Name of the file (should already be unique)

    Returns:
        Full path for blob storage (e.g., 'audio/filename.mp3')
    """
    # Remove any leading slashes
    category = category.strip('/')
    filename = filename.strip('/')

    return f"{category}/{filename}"
