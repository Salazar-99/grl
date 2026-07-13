//! vsock server: the endpoint the manager dials to forward calls into the VM.
//! The wire is a 1-byte message kind, then a 4-byte big-endian length, then an
//! encoded protobuf message. The reply is just the length-prefixed protobuf for
//! that kind's response (the caller knows what it asked). Reusing the shared
//! contract means the manager re-emits the very message it received from the
//! training client.
//!
//! Two kinds flow today: [`MsgKind::Execute`] (an [`ExecuteRequest`], the common
//! per-tool-call path) and [`MsgKind::Evaluate`] (an [`EvaluateRequest`], run once at
//! the end of a trajectory to compute the reward).
//!
//! One connection carries many requests (the manager holds it open for the life
//! of the env), so framing — not connection boundaries — delimits messages.

use std::io::{self, Read, Write};
use std::sync::Arc;
use std::time::Duration;

use prost::Message;

use crate::pb::{EvaluateRequest, ExecuteRequest, ExecuteResponse};
use crate::score;
use crate::session::Sessions;

/// Reject absurd frames before allocating for them.
const MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;

/// Fallback when a tool call doesn't specify one.
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(120);

/// Wire discriminator for the request a frame carries.
#[derive(Clone, Copy, PartialEq, Eq)]
enum MsgKind {
    Execute,
    Evaluate,
}

impl MsgKind {
    fn from_byte(b: u8) -> Option<MsgKind> {
        match b {
            0 => Some(MsgKind::Execute),
            1 => Some(MsgKind::Evaluate),
            _ => None,
        }
    }

    fn as_byte(self) -> u8 {
        match self {
            MsgKind::Execute => 0,
            MsgKind::Evaluate => 1,
        }
    }
}

/// Start serving on `listen` until the process is killed.
///
/// `listen` is either `vsock:<port>` (production: the manager dials the guest
/// CID on this port) or a `host:port` TCP address (local/off-VM dev, since
/// AF_VSOCK is Linux-only).
pub fn serve(listen: &str, sessions: Arc<Sessions>) -> io::Result<()> {
    if let Some(port) = listen.strip_prefix("vsock:") {
        let port: u32 = port
            .parse()
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "invalid vsock port"))?;
        serve_vsock(port, sessions)
    } else {
        serve_tcp(listen, sessions)
    }
}

fn serve_tcp(addr: &str, sessions: Arc<Sessions>) -> io::Result<()> {
    let listener = std::net::TcpListener::bind(addr)?;
    eprintln!("executor listening on tcp://{addr}");
    for stream in listener.incoming() {
        let stream = stream?;
        let sessions = Arc::clone(&sessions);
        std::thread::spawn(move || handle_conn(stream, &sessions));
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn serve_vsock(port: u32, sessions: Arc<Sessions>) -> io::Result<()> {
    use vsock::{VsockListener, VMADDR_CID_ANY};
    let listener = VsockListener::bind_with_cid_port(VMADDR_CID_ANY, port)?;
    eprintln!("executor listening on vsock:{port}");
    for stream in listener.incoming() {
        let stream = stream?;
        let sessions = Arc::clone(&sessions);
        std::thread::spawn(move || handle_conn(stream, &sessions));
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn serve_vsock(_port: u32, _sessions: Arc<Sessions>) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "vsock is only supported on Linux; use a host:port TCP address off-VM",
    ))
}

/// Serve requests on one connection until the peer closes it.
pub fn handle_conn<S: Read + Write>(mut stream: S, sessions: &Sessions) {
    loop {
        let kind = match read_kind(&mut stream) {
            Ok(Some(kind)) => kind,
            Ok(None) => break, // peer closed cleanly
            Err(_) => break,   // unknown kind / read error: drop the connection
        };
        let frame = match read_frame(&mut stream) {
            Ok(Some(frame)) => frame,
            Ok(None) => break, // peer closed mid-message
            Err(_) => break,   // truncated/oversized frame: drop the connection
        };
        let payload = match kind {
            MsgKind::Execute => match ExecuteRequest::decode(frame.as_slice()) {
                Ok(req) => dispatch(sessions, req).encode_to_vec(),
                Err(e) => error_response(format!("malformed ExecuteRequest: {e}")).encode_to_vec(),
            },
            MsgKind::Evaluate => match EvaluateRequest::decode(frame.as_slice()) {
                Ok(req) => score::score(sessions, &req.env_id).encode_to_vec(),
                Err(e) => score::error_score(format!("malformed EvaluateRequest: {e}"))
                    .encode_to_vec(),
            },
        };
        if write_frame(&mut stream, &payload).is_err() {
            break;
        }
    }
}

/// Map one [`ExecuteRequest`] onto the persistent shell.
fn dispatch(sessions: &Sessions, req: ExecuteRequest) -> ExecuteResponse {
    let (command, timeout) = match parse_tool(&req.tool_name, &req.arguments_json) {
        Ok(parsed) => parsed,
        Err(e) => return error_response(e),
    };
    match sessions.execute(&req.env_id, &command, timeout) {
        Ok(out) => ExecuteResponse {
            content: out.content,
            // Non-zero exit is a failed tool call, surfaced to the policy.
            is_error: out.exit_code != 0,
        },
        Err(e) => error_response(format!("execution failed: {e}")),
    }
}

/// Translate a tool name + JSON arguments into a shell command and timeout.
///
/// Today the only tool is a shell, taking `{"command": "...", "timeout_secs"?: N}`.
/// New tools (file edits, search, etc.) get their own arm here.
fn parse_tool(tool_name: &str, arguments_json: &str) -> Result<(String, Duration), String> {
    match tool_name {
        "bash" | "shell" | "sh" => {
            let args: serde_json::Value = serde_json::from_str(arguments_json)
                .map_err(|e| format!("invalid arguments_json: {e}"))?;
            let command = args
                .get("command")
                .and_then(|v| v.as_str())
                .ok_or_else(|| "missing string field \"command\"".to_string())?
                .to_string();
            let timeout = args
                .get("timeout_secs")
                .and_then(|v| v.as_u64())
                .map(Duration::from_secs)
                .unwrap_or(DEFAULT_TIMEOUT);
            Ok((command, timeout))
        }
        other => Err(format!("unknown tool: {other}")),
    }
}

fn error_response(message: String) -> ExecuteResponse {
    ExecuteResponse {
        content: message,
        is_error: true,
    }
}

/// Read the 1-byte message kind that prefixes each request. `Ok(None)` means
/// the peer closed at a message boundary (clean shutdown).
fn read_kind<R: Read>(reader: &mut R) -> io::Result<Option<MsgKind>> {
    let mut byte = [0u8; 1];
    match reader.read_exact(&mut byte) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e),
    }
    MsgKind::from_byte(byte[0]).map(Some).ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("unknown message kind {}", byte[0]),
        )
    })
}

/// Read one length-prefixed frame. `Ok(None)` means the peer closed at a frame
/// boundary (clean shutdown).
fn read_frame<R: Read>(reader: &mut R) -> io::Result<Option<Vec<u8>>> {
    let mut len_buf = [0u8; 4];
    match reader.read_exact(&mut len_buf) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e),
    }
    let len = u32::from_be_bytes(len_buf) as usize;
    if len > MAX_FRAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("frame of {len} bytes exceeds cap"),
        ));
    }
    let mut buf = vec![0u8; len];
    reader.read_exact(&mut buf)?;
    Ok(Some(buf))
}

fn write_frame<W: Write>(writer: &mut W, payload: &[u8]) -> io::Result<()> {
    writer.write_all(&(payload.len() as u32).to_be_bytes())?;
    writer.write_all(payload)?;
    writer.flush()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{TcpListener, TcpStream};

    fn write_request<W: Write>(writer: &mut W, kind: MsgKind, payload: &[u8]) {
        writer.write_all(&[kind.as_byte()]).unwrap();
        write_frame(writer, payload).unwrap();
    }

    fn execute(stream: &mut TcpStream, tool: &str, args: &str) -> ExecuteResponse {
        let req = ExecuteRequest {
            env_id: "t".into(),
            tool_name: tool.into(),
            arguments_json: args.into(),
        };
        write_request(stream, MsgKind::Execute, &req.encode_to_vec());
        let frame = read_frame(stream).unwrap().unwrap();
        ExecuteResponse::decode(frame.as_slice()).unwrap()
    }

    #[test]
    fn roundtrip_persists_state_across_framed_requests() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let sessions = Arc::new(Sessions::with_workspace("/tmp"));
        std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            handle_conn(stream, &sessions);
        });

        let mut client = TcpStream::connect(addr).unwrap();

        // First call mutates shell state.
        let r1 = execute(
            &mut client,
            "bash",
            r#"{"command":"cd /tmp && export FOO=bar"}"#,
        );
        assert!(!r1.is_error, "got: {:?}", r1.content);

        // Second call, separate frame, observes that state — proving the shell
        // (and its cwd/env) survived between requests.
        let r2 = execute(&mut client, "bash", r#"{"command":"echo \"$FOO from $(pwd)\""}"#);
        assert!(!r2.is_error, "got: {:?}", r2.content);
        assert!(
            r2.content.contains("bar from /tmp"),
            "expected persisted state, got: {:?}",
            r2.content
        );

        // Non-zero exit surfaces as is_error.
        let r3 = execute(&mut client, "bash", r#"{"command":"exit 3"}"#);
        assert!(r3.is_error);

        // Unknown tool is reported, not executed.
        let r4 = execute(&mut client, "telekinesis", "{}");
        assert!(r4.is_error);
        assert!(r4.content.contains("unknown tool"));
    }
}
