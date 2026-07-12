//! Firecracker HTTP API client over a Unix domain socket.
//!
//! Firecracker keeps API connections alive, so response completion must be
//! determined from HTTP framing rather than EOF. Hyper owns that framing; each
//! operation uses a fresh UDS connection which is dropped after one response.

use std::path::Path;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::client::conn::http1;
use hyper::header::{CONNECTION, CONTENT_TYPE, HOST};
use hyper::{Method, Request};
use hyper_util::rt::TokioIo;
use tokio::net::UnixStream;
use tokio::task::JoinHandle;
use tokio::time::{Instant, sleep, timeout};

use serde_json::Value;

const DEFAULT_API_TIMEOUT: Duration = Duration::from_secs(10);
const CONNECT_RETRY_INTERVAL: Duration = Duration::from_millis(50);
const MAX_RESPONSE_BYTES: usize = 1024 * 1024;

fn api_timeout() -> Duration {
    std::env::var("GRL_FIRECRACKER_API_TIMEOUT_SECS")
        .ok()
        .and_then(|value| value.parse().ok())
        .map(Duration::from_secs)
        .unwrap_or(DEFAULT_API_TIMEOUT)
}

async fn connect_until(socket_path: &Path, deadline: Instant) -> Result<UnixStream, String> {
    loop {
        match UnixStream::connect(socket_path).await {
            Ok(stream) => return Ok(stream),
            Err(err) if Instant::now() >= deadline => {
                return Err(format!(
                    "connect {} before deadline: {err}",
                    socket_path.display()
                ));
            }
            Err(_) => sleep(CONNECT_RETRY_INTERVAL).await,
        }
    }
}

pub async fn put(socket_path: &Path, resource: &str, body: &Value) -> Result<(), String> {
    put_with_timeout(socket_path, resource, body, api_timeout()).await
}

async fn put_with_timeout(
    socket_path: &Path,
    resource: &str,
    body: &Value,
    operation_timeout: Duration,
) -> Result<(), String> {
    timeout(
        operation_timeout,
        put_inner(socket_path, resource, body, operation_timeout),
    )
    .await
    .map_err(|_| {
        format!(
            "firecracker PUT /{resource} timed out after {:.1}s",
            operation_timeout.as_secs_f64()
        )
    })?
}

async fn put_inner(
    socket_path: &Path,
    resource: &str,
    body: &Value,
    operation_timeout: Duration,
) -> Result<(), String> {
    let payload = serde_json::to_string(body).map_err(|e| format!("encode {resource}: {e}"))?;
    let stream = connect_until(socket_path, Instant::now() + operation_timeout).await?;
    let (mut sender, connection) = http1::handshake(TokioIo::new(stream))
        .await
        .map_err(|e| format!("HTTP handshake for /{resource}: {e}"))?;
    let connection = ConnectionTask::spawn(connection);

    let request = Request::builder()
        .method(Method::PUT)
        .uri(format!("/{resource}"))
        .header(HOST, "localhost")
        .header(CONTENT_TYPE, "application/json")
        .header(CONNECTION, "close")
        .body(Full::new(Bytes::from(payload)))
        .map_err(|e| format!("build request for /{resource}: {e}"))?;

    let response = sender
        .send_request(request)
        .await
        .map_err(|e| format!("send PUT /{resource}: {e}"))?;
    let status = response.status();
    let response_body = Limited::new(response.into_body(), MAX_RESPONSE_BYTES)
        .collect()
        .await
        .map_err(|e| format!("read PUT /{resource} response body: {e}"))?
        .to_bytes();

    // Close the keep-alive connection only after Hyper has observed a complete
    // framed response. In particular, never SHUT_WR before Firecracker replies.
    drop(sender);
    drop(connection);

    if status.is_success() {
        return Ok(());
    }
    let detail = String::from_utf8_lossy(&response_body);
    Err(format!(
        "firecracker PUT /{resource} failed: {status}{}{}",
        if detail.is_empty() { "" } else { ": " },
        detail.trim()
    ))
}

struct ConnectionTask(JoinHandle<()>);

impl ConnectionTask {
    fn spawn<I>(connection: http1::Connection<I, Full<Bytes>>) -> Self
    where
        I: hyper::rt::Read + hyper::rt::Write + Unpin + Send + 'static,
    {
        Self(tokio::spawn(async move {
            let _ = connection.await;
        }))
    }
}

impl Drop for ConnectionTask {
    fn drop(&mut self) {
        self.0.abort();
    }
}

#[cfg(test)]
mod tests {
    use std::io::ErrorKind;
    #[cfg(target_os = "linux")]
    use std::path::Path;
    use std::path::PathBuf;
    #[cfg(target_os = "linux")]
    use std::process::Stdio;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::Duration;

    use hyper::StatusCode;
    use serde_json::json;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::UnixListener;
    use tokio::time::sleep;

    use super::put_with_timeout;

    static NEXT_SOCKET: AtomicU64 = AtomicU64::new(0);

    fn socket_path() -> PathBuf {
        std::env::temp_dir().join(format!(
            "grl-firecracker-test-{}-{}.sock",
            std::process::id(),
            NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
        ))
    }

    async fn read_request(stream: &mut tokio::net::UnixStream) -> Vec<u8> {
        let mut request = Vec::new();
        let mut chunk = [0u8; 256];
        loop {
            let n = stream.read(&mut chunk).await.unwrap();
            assert_ne!(n, 0, "client half-closed before request completed");
            request.extend_from_slice(&chunk[..n]);
            if let Some(header_end) = request.windows(4).position(|w| w == b"\r\n\r\n") {
                let headers = String::from_utf8_lossy(&request[..header_end]);
                let content_length: usize = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse().unwrap())
                    })
                    .unwrap();
                if request.len() >= header_end + 4 + content_length {
                    return request;
                }
            }
        }
    }

    #[tokio::test]
    async fn keep_alive_response_completes_without_waiting_for_eof() {
        let path = socket_path();
        let listener = UnixListener::bind(&path).unwrap();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let request = read_request(&mut stream).await;
            assert!(String::from_utf8_lossy(&request).starts_with("PUT /machine-config "));
            // Emulate Firecracker's behavior: it treats a write-side EOF before
            // its response as a disconnected API client.
            sleep(Duration::from_millis(10)).await;
            match stream.try_read(&mut [0u8; 1]) {
                Err(err) if err.kind() == ErrorKind::WouldBlock => {}
                Ok(0) => return,
                other => panic!("unexpected client state before response: {other:?}"),
            }
            stream
                .write_all(
                    b"HTTP/1.1 204 \r\nServer: Firecracker API\r\nConnection: keep-alive\r\n\r\n",
                )
                .await
                .unwrap();
            sleep(Duration::from_secs(1)).await;
        });

        put_with_timeout(
            &path,
            "machine-config",
            &json!({"vcpu_count": 2}),
            Duration::from_millis(250),
        )
        .await
        .unwrap();
        server.abort();
        let _ = std::fs::remove_file(path);
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    #[ignore = "requires the Firecracker binary; run inside the manager image"]
    async fn actual_firecracker_machine_config_smoke() {
        let path = socket_path();
        let binary = super::super::jailer::firecracker_bin();
        assert!(
            Path::new(&binary).is_file(),
            "Firecracker binary not found at {binary}"
        );
        let mut child = tokio::process::Command::new(binary)
            .args(["--api-sock", path.to_str().unwrap()])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()
            .unwrap();

        let result = put_with_timeout(
            &path,
            "machine-config",
            &json!({"vcpu_count": 1, "mem_size_mib": 128, "smt": false}),
            Duration::from_secs(5),
        )
        .await;
        let _ = child.start_kill();
        let _ = child.wait().await;
        let _ = std::fs::remove_file(path);
        result.unwrap();
    }

    #[tokio::test]
    async fn fragmented_error_response_includes_body() {
        let path = socket_path();
        let listener = UnixListener::bind(&path).unwrap();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            read_request(&mut stream).await;
            stream
                .write_all(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 16\r\n\r\nbad ")
                .await
                .unwrap();
            stream.write_all(b"request body").await.unwrap();
        });

        let err = put_with_timeout(
            &path,
            "machine-config",
            &json!({"vcpu_count": 2}),
            Duration::from_secs(1),
        )
        .await
        .unwrap_err();
        assert!(err.contains(StatusCode::BAD_REQUEST.as_str()));
        assert!(err.contains("bad request body"));
        server.await.unwrap();
        let _ = std::fs::remove_file(path);
    }

    #[tokio::test]
    async fn response_timeout_is_bounded() {
        let path = socket_path();
        let listener = UnixListener::bind(&path).unwrap();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            read_request(&mut stream).await;
            sleep(Duration::from_secs(1)).await;
        });

        let err = put_with_timeout(
            &path,
            "machine-config",
            &json!({"vcpu_count": 2}),
            Duration::from_millis(50),
        )
        .await
        .unwrap_err();
        assert!(err.contains("timed out"));
        server.abort();
        let _ = std::fs::remove_file(path);
    }
}
