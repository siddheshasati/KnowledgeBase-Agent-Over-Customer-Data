# Deployment Troubleshooting Guide

This guide covers the common errors you'll encounter during deployment and how to fix them.

---

## Issue 1: Cohere API 429 - Too Many Requests

**Error:**
```
httpx HTTP Request: POST https://api.cohere.com/v1/embed "HTTP/1.1 429 Too Many Requests"
WARNING app.services.embeddings Cohere embeddings failed; falling back to deterministic local vectors
```

**Root cause:**
- Cohere free tier has strict rate limits (10 requests/minute)
- Ingestion tries to embed ALL records at once
- Each batch retry consumes quota quickly

**Why it's OK:**
The app correctly falls back to deterministic local vectors, so retrieval still works. This is by design.

**How to avoid in production:**

### Option A: Use a better embedding service
```env
# Switch to OpenAI if you have quota
EMBEDDING_PROVIDER=openai
OPENAI_API_KEY=your_key

# Or use a self-hosted model
EMBEDDING_PROVIDER=local
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
```

### Option B: Rate-limit ingestion
Edit `app/ingestion/sync.py`:

```python
async def _upsert_record(self, record):
    # Add delay between records to respect rate limits
    await asyncio.sleep(0.5)  # 500ms between each embed call
    
    # Or batch them
    if len(self.pending_embeddings) >= 10:
        await self._flush_embeddings()
```

### Option C: Increase embedding batch size (if provider allows)
Edit `app/services/embeddings.py`:

```python
async def embed(self, texts, input_type="search_query"):
    # Batch up to 100 texts at once instead of 1
    if isinstance(texts, str):
        texts = [texts]
    
    # Embed all together
    response = await self.client.embed(
        texts=texts,
        input_type=input_type,
        model=self.settings.cohere_embed_model
    )
```

### For now: Disable Cohere during dev/ingestion
```env
# Temporarily unset to use local vectors only
# COHERE_API_KEY=
```

---

## Issue 2: Qdrant Connection Lost

**Error:**
```
qdrant_client.http.exceptions.ResponseHandlingException: All connection attempts failed
httpcore.ConnectError: All connection attempts failed
```

**Root cause:**
- Qdrant service crashed or became unreachable
- Network timeout or port issue
- Too many requests to Qdrant from ingestion

**How to recover:**

### Check Qdrant status
```powershell
# Test connection
Invoke-WebRequest -Uri http://127.0.0.1:6333 -ErrorAction SilentlyContinue

# If offline, restart it
docker compose restart qdrant

# Or restart all services
docker compose down
docker compose up -d
```

### Increase timeouts for Qdrant
Edit `app/config.py`:

```python
qdrant_timeout: int = 30  # Increase from default 10s
qdrant_pool_size: int = 10  # Increase connection pool

class Settings(BaseSettings):
    # ... add these
    qdrant_timeout: int = 30
```

### Implement reconnection logic
Edit `app/db/qdrant_store.py`:

```python
from tenacity import retry, stop_after_attempt, wait_exponential

class QdrantVectorStore:
    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), 
           stop=stop_after_attempt(3))
    async def search(self, embedding, top_k):
        # Automatically retry on connection failure
        return await self.client.search(...)
```

---

## Issue 3: Neo4j Duplicate Constraint Violation

**Error:**
```
Neo.ClientError.Schema.ConstraintValidationFailed
Node(1225) already exists with label `Chunk` and property `id` = 'issue:iss-0300:chunk:0'
```

**Root cause:**
- Upsert is trying to create duplicate chunk IDs
- Hash collision or idempotence issue in chunk generation
- Transaction not properly handling retries

**How to fix:**

### Update Neo4j upsert logic
Edit `app/db/neo4j_store.py`:

```python
async def upsert_chunks(self, record_id: str, chunks: list[Chunk]):
    async with self.driver.session() as session:
        # Use MERGE with proper error handling
        query = """
        UNWIND $chunks AS chunk
        MERGE (c:Chunk {id: chunk.id})
        ON CREATE SET 
            c.text = chunk.text,
            c.title = chunk.title,
            c.created_at = datetime()
        ON MATCH SET 
            c.text = chunk.text,
            c.title = chunk.title,
            c.updated_at = datetime()
        WITH c
        MATCH (r:SourceRecord {id: $record_id})
        MERGE (r)-[:HAS_CHUNK]->(c)
        """
        
        try:
            await session.run(query, 
                record_id=record_id,
                chunks=[c.model_dump() for c in chunks]
            )
        except Exception as exc:
            # Log and skip duplicate, don't fail whole ingestion
            logger.warning(f"Chunk upsert warning for {record_id}: {exc}")
```

### Alternative: Soft delete before upsert
```python
async def upsert_record(self, record):
    # Delete old chunks first
    await self.vectors.delete_record_chunks([record.id])
    
    # Then recreate
    await self.neo4j.upsert_chunks(record.id, record.chunks)
```

---

## Issue 4: Ingestion Failures Cascading

**Root cause:**
When one service fails (Cohere, Qdrant, Neo4j), the entire ingestion run fails instead of continuing.

**How to fix:**

### Make ingestion resilient
Edit `app/ingestion/sync.py`:

```python
async def _upsert_record(self, record):
    try:
        # Try Neo4j upsert
        await self.neo4j.upsert_record(record)
        self.run.upserted_records += 1
    except Exception as exc:
        logger.warning(f"Neo4j upsert failed for {record.id}: {exc}")
        self.run.errors.append(str(exc))
        # Don't crash, continue to next record
    
    try:
        # Try vector upsert
        await self.vectors.upsert_chunks(record.id, record.chunks)
    except Exception as exc:
        logger.warning(f"Vector upsert failed for {record.id}: {exc}")
        self.run.errors.append(str(exc))
        # Don't crash, continue to next record
```

This way, if Cohere rate-limits but Neo4j works, you still get some progress.

---

## Production Deployment Checklist

Before deploying to Render/Vercel:

### API Keys & Services
- [ ] Verify GROQ_API_KEY is valid and has quota
- [ ] Verify COHERE_API_KEY (or remove to use local embeddings)
- [ ] Verify Neo4j credentials and connectivity
- [ ] Verify Qdrant is reachable and responsive
- [ ] Set POSTGRES_DSN or plan to use SQLite fallback

### Limits & Timeouts
- [ ] Set reasonable LIVE_FETCH_TIMEOUT_SECONDS (12s is default)
- [ ] Set EVIDENCE_TOP_K conservatively (8 is default)
- [ ] Limit LIVE_MAX_PAGES (6 is default)

### Fallback Modes
- [ ] Confirm SQLite fallback is working for chat history
- [ ] Confirm local vector fallback works if Cohere is down
- [ ] Confirm Neo4j fallback returns empty results gracefully

### Rate Limiting
- [ ] Space out ingestion calls (don't run every minute)
- [ ] Monitor Cohere/Groq API usage daily
- [ ] Set up alerts for 429 errors

### Monitoring
- [ ] Check `/health` endpoint regularly
- [ ] Monitor ingestion errors in logs
- [ ] Alert if any service shows degraded status

---

## If Deployment Fails on Render

### Common Render issues

1. **Timeout during dependency install**
   - Solution: Use `--prefer-binary` flag (already in render.yaml)
   - Or reduce dependencies

2. **Out of memory during build**
   - Solution: Reduce build workers
   - Or simplify requirements.txt

3. **Service fails to start**
   - Check logs: `render logs <service-name>`
   - Verify environment variables are set
   - Test locally first: `gunicorn -w 1 -k uvicorn.workers.UvicornWorker app.main:app`

4. **Neo4j/Qdrant not reachable from Render**
   - Solution: Use managed services with public endpoints
   - Or use VPC networking (paid feature)
   - For free tier: keep local development, use Render for API only

---

## Recommended Free-Tier Setup

To minimize deployment issues on free tier:

1. **Keep Neo4j/Qdrant local** (or use small managed instance)
2. **Deploy only the FastAPI backend to Render**
3. **Use SQLite for chat history** (automatic fallback)
4. **Use local embeddings** (disable Cohere if quota issues)
5. **Don't run ingestion on Render** (do it locally, upload data)
6. **Keep API usage light** (few requests/min)

This approach avoids most of the issues above.

---

## Quick Recovery Steps

If your deployment breaks:

1. Check `/health` endpoint
   ```powershell
   Invoke-WebRequest http://your-api.com/health
   ```

2. Review logs for specific service failures
   - Neo4j: connection refused?
   - Qdrant: connection refused?
   - Cohere: 429 rate limit?

3. Restart services in order:
   ```powershell
   docker compose restart qdrant
   docker compose restart neo4j
   # Then restart your app
   ```

4. Clear bad data if needed:
   ```powershell
   # Delete Qdrant collections
   curl -X DELETE http://127.0.0.1:6333/collections/kb_agent_chunks
   
   # Clear Neo4j (be careful!)
   # Only do this if absolutely necessary
   ```

5. Re-run ingestion:
   ```powershell
   python scripts/ingest_once.py
   ```

---

## Testing Before Production

Test these scenarios locally before deploying:

```bash
# Test health check
curl http://127.0.0.1:8000/health

# Test chat endpoint
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Test question"}'

# Test with Cohere disabled
unset COHERE_API_KEY
python -m uvicorn app.main:app

# Test with Neo4j down
docker compose stop neo4j
curl http://127.0.0.1:8000/health  # Should show neo4j: not_connected

# Test with Qdrant down  
docker compose stop qdrant
curl http://127.0.0.1:8000/health  # Should show qdrant: not_connected
```

All these should return sensible responses, not 500 errors.

---

## Summary

| Error | Cause | Fix |
|---|---|---|
| 429 Too Many Requests | Cohere rate limit | Use local embeddings or batch requests |
| Connection refused | Service down | Restart service or use managed provider |
| Duplicate constraint | ID collision | Use MERGE with ON MATCH/ON CREATE |
| Ingestion fails | Single service failure | Make ingestion resilient, continue on error |
| Out of memory | Service too large | Reduce workers or dependencies |
| Timeout | Slow network | Increase timeouts, check connectivity |

The key principle: **fail gracefully, don't crash the whole app for one service outage.**
