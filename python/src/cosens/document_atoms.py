"""Document upload and status endpoints."""

_ALLOWED_FIELDS = {'thread_id', 'memory_scope', 'uploaded_at', 'source_kind', 'workspace_id',
                   'ingest_mode', 'summary', 'background', 'wait_for_processing', 'wait_timeout_seconds', 'debug'}


def _upload_args(filename, mime_type, content, metadata):
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError('filename must be nonempty')
    if not isinstance(mime_type, str) or not mime_type.strip():
        raise ValueError('mime_type must be nonempty')
    if not isinstance(content, bytes) or not content:
        raise ValueError('content must be nonempty bytes')
    if metadata.keys() - _ALLOWED_FIELDS:
        raise ValueError('unsupported document metadata')
    return {k: v for k, v in metadata.items() if v is not None}


def _document_ids(ids):
    if not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item.strip() for item in ids):
        raise ValueError('document_ids must be a nonempty list of nonempty strings')
    return {'document_ids': ids}


class AsyncDocumentAtoms:
    async def upload_document(self, *, filename, mime_type, content, **metadata):
        fields = _upload_args(filename, mime_type, content, metadata)
        return await self._http.request_multipart('POST', '/v1/document/upload', filename=filename, mime_type=mime_type, content=content, fields=fields)

    async def get_document_status(self, document_ids):
        return await self._http.request('POST', '/v1/document/status', body=_document_ids(document_ids))


class SyncDocumentAtoms:
    def upload_document(self, *, filename, mime_type, content, **metadata):
        fields = _upload_args(filename, mime_type, content, metadata)
        return self._http.request_multipart('POST', '/v1/document/upload', filename=filename, mime_type=mime_type, content=content, fields=fields)

    def get_document_status(self, document_ids):
        return self._http.request('POST', '/v1/document/status', body=_document_ids(document_ids))
