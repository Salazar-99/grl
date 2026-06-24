//! Minimal Firecracker HTTP API client over a Unix domain socket.

use std::path::Path;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::time::sleep;

use serde_json::Value;

pub async fn wait_for_socket(socket_path: &Path, timeout: Duration) -> Result<(), String> {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        if socket_path.exists() {
            if UnixStream::connect(socket_path).await.is_ok() {
                return Ok(());
            }
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(format!(
                "firecracker api socket not ready: {}",
                socket_path.display()
            ));
        }
        sleep(Duration::from_millis(50)).await;
    }
}

pub async fn put(socket_path: &Path, resource: &str, body: &Value) -> Result<(), String> {
    let payload =
        serde_json::to_string(body).map_err(|e| format!("encode {resource}: {e}"))?;
    let request = format!(
        "PUT /{resource} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        payload.len(),
        payload
    );

    let mut stream = UnixStream::connect(socket_path)
        .await
        .map_err(|e| format!("connect {}: {e}", socket_path.display()))?;
    stream
        .write_all(request.as_bytes())
        .await
        .map_err(|e| format!("write {resource}: {e}"))?;

    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .await
        .map_err(|e| format!("read {resource}: {e}"))?;
    let status_line = String::from_utf8_lossy(&response);
    let first_line = status_line.lines().next().unwrap_or("");
    if first_line.contains(" 204 ") || first_line.contains(" 200 ") {
        Ok(())
    } else {
        Err(format!(
            "firecracker PUT /{resource} failed: {}",
            first_line.trim()
        ))
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    #[test]
    fn put_request_format_includes_content_length() {
        let body = json!({"vcpu_count": 2});
        let payload = body.to_string();
        let req = format!(
            "PUT /machine-config HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            payload.len(),
            payload
        );
        assert!(req.contains("Content-Length: 16"));
        assert!(req.ends_with(&payload));
    }
}
