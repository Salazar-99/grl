//! Minimal Firecracker HTTP API client over a Unix domain socket.
//!
//! Firecracker answers with `Connection: keep-alive` and leaves the UDS open,
//! so callers must parse a single response and drop the stream — never
//! `read_to_end`, which waits forever for EOF.

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
            // Connect then immediately shut down so Firecracker's single-client
            // API loop is not left holding an idle accepted connection.
            if let Ok(mut stream) = UnixStream::connect(socket_path).await {
                let _ = stream.shutdown().await;
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
    // Half-close write so FC knows the request is complete even if it ignores
    // Connection: close (it does — responses are keep-alive).
    let _ = stream.shutdown().await;

    let response = read_http_response(&mut stream)
        .await
        .map_err(|e| format!("read {resource}: {e}"))?;
    let first_line = response.lines().next().unwrap_or("");
    if first_line.contains(" 204 ") || first_line.contains(" 200 ") {
        Ok(())
    } else {
        Err(format!(
            "firecracker PUT /{resource} failed: {}",
            first_line.trim()
        ))
    }
}

/// Read one HTTP/1.x response without waiting for the peer to close.
async fn read_http_response(stream: &mut UnixStream) -> Result<String, String> {
    let mut buf = Vec::with_capacity(256);
    let mut tmp = [0u8; 256];
    loop {
        let n = stream
            .read(&mut tmp)
            .await
            .map_err(|e| format!("socket read: {e}"))?;
        if n == 0 {
            return Err("connection closed before HTTP headers finished".into());
        }
        buf.extend_from_slice(&tmp[..n]);
        if let Some(header_end) = find_header_end(&buf) {
            let headers = std::str::from_utf8(&buf[..header_end])
                .map_err(|e| format!("response headers not utf-8: {e}"))?;
            let content_length = content_length(headers).unwrap_or(0);
            let body_start = header_end + 4; // skip \r\n\r\n
            while buf.len() < body_start + content_length {
                let n = stream
                    .read(&mut tmp)
                    .await
                    .map_err(|e| format!("socket read body: {e}"))?;
                if n == 0 {
                    return Err("connection closed before HTTP body finished".into());
                }
                buf.extend_from_slice(&tmp[..n]);
            }
            return Ok(String::from_utf8_lossy(&buf[..body_start + content_length]).into_owned());
        }
        if buf.len() > 64 * 1024 {
            return Err("HTTP headers exceeded 64KiB".into());
        }
    }
}

fn find_header_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n")
}

fn content_length(headers: &str) -> Option<usize> {
    headers.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        if name.eq_ignore_ascii_case("content-length") {
            value.trim().parse().ok()
        } else {
            None
        }
    })
}

#[cfg(test)]
mod tests {
    use super::{content_length, find_header_end};
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

    #[test]
    fn finds_header_terminator() {
        let msg = b"HTTP/1.1 204 \r\nConnection: keep-alive\r\n\r\n";
        assert_eq!(find_header_end(msg), Some(msg.len() - 4));
    }

    #[test]
    fn parses_content_length_case_insensitive() {
        let headers = "HTTP/1.1 200 OK\r\nContent-Length: 12\r\n";
        assert_eq!(content_length(headers), Some(12));
        let headers = "HTTP/1.1 204 \r\nConnection: keep-alive\r\n";
        assert_eq!(content_length(headers), None);
    }
}
