//! Framed protobuf relay to the in-VM executor (vsock in production, TCP in tests).

use std::io::{self, Read, Write};
use std::net::TcpStream;
#[cfg(target_os = "linux")]
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use prost::Message;

use crate::pb::{EvaluateRequest, EvaluateResponse, ExecuteRequest, ExecuteResponse};

pub const MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;
const EVALUATE_TIMEOUT: Duration = Duration::from_secs(920);
const DEFAULT_TOOL_TIMEOUT_SECS: u64 = 120;
const EXECUTE_TIMEOUT_BUFFER_SECS: u64 = 10;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum MsgKind {
    Execute = 0,
    Evaluate = 1,
}

enum Transport {
    Tcp(TcpStream),
    #[cfg(target_os = "linux")]
    Vsock(vsock::VsockStream),
}

impl Read for Transport {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            Transport::Tcp(stream) => stream.read(buf),
            #[cfg(target_os = "linux")]
            Transport::Vsock(stream) => stream.read(buf),
        }
    }
}

impl Write for Transport {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            Transport::Tcp(stream) => stream.write(buf),
            #[cfg(target_os = "linux")]
            Transport::Vsock(stream) => stream.write(buf),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        match self {
            Transport::Tcp(stream) => stream.flush(),
            #[cfg(target_os = "linux")]
            Transport::Vsock(stream) => stream.flush(),
        }
    }
}

impl Transport {
    fn set_timeouts(&mut self, timeout: Duration) -> io::Result<()> {
        match self {
            Transport::Tcp(stream) => {
                stream.set_read_timeout(Some(timeout))?;
                stream.set_write_timeout(Some(timeout))?;
            }
            #[cfg(target_os = "linux")]
            Transport::Vsock(stream) => {
                stream.set_read_timeout(Some(timeout))?;
                stream.set_write_timeout(Some(timeout))?;
            }
        }
        Ok(())
    }
}

/// One persistent connection to the in-VM executor for a rollout environment.
pub struct ExecutorConn {
    stream: Arc<Mutex<Transport>>,
}

impl std::fmt::Debug for ExecutorConn {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ExecutorConn").finish_non_exhaustive()
    }
}

impl ExecutorConn {
    #[cfg(target_os = "linux")]
    pub fn connect_vsock_timeout(guest_cid: u32, timeout: Duration) -> Result<Self, String> {
        use crate::vm::config::EXECUTOR_VSOCK_PORT;

        let stream =
            connect_vsock_nonblocking(guest_cid, EXECUTOR_VSOCK_PORT, timeout).map_err(|e| {
                format!("vsock connect cid={guest_cid} port={EXECUTOR_VSOCK_PORT}: {e}")
            })?;
        Ok(Self {
            stream: Arc::new(Mutex::new(Transport::Vsock(stream))),
        })
    }

    pub fn connect_tcp(addr: &str) -> Result<Self, String> {
        let stream = TcpStream::connect(addr).map_err(|e| format!("tcp connect {addr}: {e}"))?;
        Ok(Self {
            stream: Arc::new(Mutex::new(Transport::Tcp(stream))),
        })
    }

    pub async fn forward_execute(&self, req: ExecuteRequest) -> Result<ExecuteResponse, String> {
        let timeout = execute_timeout(&req);
        let payload = req.encode_to_vec();
        let stream = Arc::clone(&self.stream);
        tokio::task::spawn_blocking(move || {
            let mut guard = stream.lock().unwrap();
            let frame = call(MsgKind::Execute, &mut guard, &payload, timeout)?;
            ExecuteResponse::decode(frame.as_slice())
                .map_err(|e| format!("decode ExecuteResponse: {e}"))
        })
        .await
        .map_err(|e| format!("executor task join: {e}"))?
    }

    pub async fn forward_evaluate(&self, env_id: &str) -> Result<EvaluateResponse, String> {
        let req = EvaluateRequest {
            env_id: env_id.to_string(),
        };
        let payload = req.encode_to_vec();
        let stream = Arc::clone(&self.stream);
        tokio::task::spawn_blocking(move || {
            let mut guard = stream.lock().unwrap();
            let frame = call(MsgKind::Evaluate, &mut guard, &payload, EVALUATE_TIMEOUT)?;
            let vm = EvaluateResponse::decode(frame.as_slice())
                .map_err(|e| format!("decode EvaluateResponse: {e}"))?;
            Ok(map_evaluate(vm))
        })
        .await
        .map_err(|e| format!("executor task join: {e}"))?
    }
}

#[cfg(target_os = "linux")]
fn connect_vsock_nonblocking(
    guest_cid: u32,
    port: u32,
    timeout: Duration,
) -> io::Result<vsock::VsockStream> {
    let raw_fd = unsafe {
        libc::socket(
            libc::AF_VSOCK,
            libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
            0,
        )
    };
    if raw_fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let fd = unsafe { OwnedFd::from_raw_fd(raw_fd) };
    let mut address: libc::sockaddr_vm = unsafe { std::mem::zeroed() };
    address.svm_family = libc::AF_VSOCK as libc::sa_family_t;
    address.svm_cid = guest_cid;
    address.svm_port = port;

    let rc = unsafe {
        libc::connect(
            fd.as_raw_fd(),
            (&raw const address).cast::<libc::sockaddr>(),
            std::mem::size_of::<libc::sockaddr_vm>() as libc::socklen_t,
        )
    };
    if rc < 0 {
        let err = io::Error::last_os_error();
        if err.raw_os_error() != Some(libc::EINPROGRESS) {
            return Err(err);
        }

        let mut poll_fd = libc::pollfd {
            fd: fd.as_raw_fd(),
            events: libc::POLLOUT,
            revents: 0,
        };
        let timeout_ms = timeout.as_millis().clamp(1, i32::MAX as u128) as i32;
        loop {
            let ready = unsafe { libc::poll(&mut poll_fd, 1, timeout_ms) };
            if ready == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "vsock connect timed out",
                ));
            }
            if ready < 0 {
                let err = io::Error::last_os_error();
                if err.kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(err);
            }
            break;
        }

        let mut socket_error: libc::c_int = 0;
        let mut error_len = std::mem::size_of_val(&socket_error) as libc::socklen_t;
        if unsafe {
            libc::getsockopt(
                fd.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_ERROR,
                (&raw mut socket_error).cast(),
                &mut error_len,
            )
        } < 0
        {
            return Err(io::Error::last_os_error());
        }
        if socket_error != 0 {
            return Err(io::Error::from_raw_os_error(socket_error));
        }
    }

    let flags = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_GETFL) };
    if flags < 0
        || unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_SETFL, flags & !libc::O_NONBLOCK) } < 0
    {
        return Err(io::Error::last_os_error());
    }

    Ok(unsafe { vsock::VsockStream::from_raw_fd(fd.into_raw_fd()) })
}

fn map_evaluate(vm: EvaluateResponse) -> EvaluateResponse {
    EvaluateResponse {
        infra_error: detail_is_infra_error(&vm.detail_json),
        ..vm
    }
}

fn detail_is_infra_error(detail_json: &str) -> bool {
    if detail_json.is_empty() {
        return true;
    }
    let Ok(value) = serde_json::from_str::<serde_json::Value>(detail_json) else {
        return true;
    };
    value.get("error").is_some()
}

fn execute_timeout(req: &ExecuteRequest) -> Duration {
    let secs = serde_json::from_str::<serde_json::Value>(&req.arguments_json)
        .ok()
        .and_then(|v| v.get("timeout_secs").and_then(|t| t.as_u64()))
        .unwrap_or(DEFAULT_TOOL_TIMEOUT_SECS);
    Duration::from_secs(secs.saturating_add(EXECUTE_TIMEOUT_BUFFER_SECS))
}

fn call(
    kind: MsgKind,
    stream: &mut Transport,
    payload: &[u8],
    timeout: Duration,
) -> Result<Vec<u8>, String> {
    stream
        .set_timeouts(timeout)
        .map_err(|e| format!("set stream timeouts: {e}"))?;
    write_request(stream, kind, payload)?;
    read_frame(stream)
}

fn write_request(stream: &mut Transport, kind: MsgKind, payload: &[u8]) -> Result<(), String> {
    stream
        .write_all(&[kind as u8])
        .map_err(|e| format!("write message kind: {e}"))?;
    stream
        .write_all(&(payload.len() as u32).to_be_bytes())
        .map_err(|e| format!("write frame length: {e}"))?;
    stream
        .write_all(payload)
        .map_err(|e| format!("write frame payload: {e}"))?;
    stream.flush().map_err(|e| format!("flush request: {e}"))
}

fn read_frame(stream: &mut Transport) -> Result<Vec<u8>, String> {
    let mut len_buf = [0u8; 4];
    match stream.read_exact(&mut len_buf) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => {
            return Err("executor closed connection".into());
        }
        Err(e) => return Err(format!("read frame length: {e}")),
    }
    let len = u32::from_be_bytes(len_buf) as usize;
    if len > MAX_FRAME_BYTES {
        return Err(format!("frame of {len} bytes exceeds cap"));
    }
    let mut buf = vec![0u8; len];
    stream
        .read_exact(&mut buf)
        .map_err(|e| format!("read frame payload: {e}"))?;
    Ok(buf)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::sync::Arc;
    use std::thread;

    use env::server::handle_conn;
    use env::session::Sessions;

    #[tokio::test]
    async fn forward_execute_roundtrip_over_tcp() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let sessions = Arc::new(Sessions::default());
        thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            handle_conn(stream, &sessions);
        });

        let conn = ExecutorConn::connect_tcp(&addr.to_string()).unwrap();
        let resp = conn
            .forward_execute(ExecuteRequest {
                env_id: "t1-0".into(),
                tool_name: "bash".into(),
                arguments_json: r#"{"command":"echo hello"}"#.into(),
            })
            .await
            .unwrap();
        assert!(!resp.is_error, "unexpected error content: {}", resp.content);
        assert!(resp.content.contains("hello"));
    }

    #[test]
    fn detail_is_infra_error_detects_scorer_errors() {
        assert!(detail_is_infra_error(
            r#"{"error":"load task spec: no such file"}"#
        ));
        assert!(!detail_is_infra_error(
            r#"{"resolved":false,"total":2,"passed":1,"failed":["t"],"missing":[]}"#
        ));
    }
}
