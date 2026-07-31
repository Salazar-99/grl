use std::io::{self, Read, Write};

use grl_string_reverse::{Task, evaluate_final_message_json, initialize};
use prost::Message;

mod pb {
    include!(concat!(env!("OUT_DIR"), "/grl.environment.v1.rs"));
}

const MAX_FRAME: usize = 16 * 1024 * 1024;

fn read_frame<R: Read>(reader: &mut R) -> io::Result<Option<Vec<u8>>> {
    let mut length = [0_u8; 4];
    match reader.read_exact(&mut length) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(error) => return Err(error),
    }
    let length = u32::from_be_bytes(length) as usize;
    if length > MAX_FRAME {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "frame exceeds limit",
        ));
    }
    let mut payload = vec![0; length];
    reader.read_exact(&mut payload)?;
    Ok(Some(payload))
}

fn write_frame<W: Write>(writer: &mut W, payload: &[u8]) -> io::Result<()> {
    writer.write_all(&(payload.len() as u32).to_be_bytes())?;
    writer.write_all(payload)?;
    writer.flush()
}

fn handle<S: Read + Write>(mut stream: S) -> io::Result<()> {
    let mut task: Option<Task> = None;
    loop {
        let mut kind = [0_u8; 1];
        match stream.read_exact(&mut kind) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(()),
            Err(error) => return Err(error),
        }
        let Some(payload) = read_frame(&mut stream)? else {
            return Ok(());
        };
        let response = match kind[0] {
            2 => {
                if task.is_some() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "duplicate initialization",
                    ));
                }
                let request = pb::InitializeRequest::decode(payload.as_slice())
                    .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
                let (initialized, prompt, tools) = initialize(&request.task_payload_json)
                    .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
                task = Some(initialized);
                pb::InitializeResponse {
                    initial_messages_json: serde_json::json!([{"role":"user","content":prompt}])
                        .to_string(),
                    tools_json: tools,
                }
                .encode_to_vec()
            }
            1 => {
                let request = pb::EvaluateRequest::decode(payload.as_slice())
                    .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
                let (reward, detail) = task
                    .as_ref()
                    .map(|task| evaluate_final_message_json(task, &request.final_message_json))
                    .unwrap_or((
                        0.0,
                        serde_json::json!({"error":"not initialized"}).to_string(),
                    ));
                pb::EvaluateResponse {
                    reward,
                    detail_json: detail,
                    infra_error: false,
                }
                .encode_to_vec()
            }
            0 => {
                let _ = pb::ExecuteRequest::decode(payload.as_slice())
                    .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
                pb::ExecuteResponse {
                    content: "no tools available".into(),
                    is_error: true,
                }
                .encode_to_vec()
            }
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "unknown message kind",
                ));
            }
        };
        write_frame(&mut stream, &response)?;
    }
}

fn serve_tcp(address: &str) -> io::Result<()> {
    let listener = std::net::TcpListener::bind(address)?;
    for stream in listener.incoming() {
        std::thread::spawn(move || {
            let _ = handle(stream.expect("accept guest connection"));
        });
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn serve_vsock(port: u32) -> io::Result<()> {
    use vsock::{VMADDR_CID_ANY, VsockListener};
    let listener = VsockListener::bind_with_cid_port(VMADDR_CID_ANY, port)?;
    for stream in listener.incoming() {
        std::thread::spawn(move || {
            let _ = handle(stream.expect("accept guest connection"));
        });
    }
    Ok(())
}

fn main() -> io::Result<()> {
    let address = std::env::var("GRL_ENV_SERVER_ADDR").unwrap_or_else(|_| "vsock:5005".into());
    if let Some(port) = address.strip_prefix("vsock:") {
        #[cfg(target_os = "linux")]
        {
            return serve_vsock(
                port.parse().map_err(|_| {
                    io::Error::new(io::ErrorKind::InvalidInput, "invalid vsock port")
                })?,
            );
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = port;
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "vsock requires Linux",
            ));
        }
    }
    serve_tcp(&address)
}
