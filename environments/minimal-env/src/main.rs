//! Minimal second managed environment used to verify the generic boot boundary.

use std::io::{self, Read, Write};

use prost::Message;

mod pb {
    #![allow(dead_code)]
    include!(concat!(env!("OUT_DIR"), "/grl.environment.v1.rs"));
}

const MAX_FRAME: usize = 1024 * 1024;

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
    loop {
        let mut kind = [0_u8; 1];
        if let Err(error) = stream.read_exact(&mut kind) {
            return if error.kind() == io::ErrorKind::UnexpectedEof {
                Ok(())
            } else {
                Err(error)
            };
        }
        let Some(payload) = read_frame(&mut stream)? else {
            return Ok(());
        };
        let response = match kind[0] {
            0 => {
                let request = pb::ExecuteRequest::decode(payload.as_slice())
                    .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
                let content = match request.tool_name.as_str() {
                    "conformance_write" => {
                        std::fs::write("/conformance-isolation", &request.arguments_json)?;
                        "written".to_string()
                    }
                    "conformance_read" => std::fs::read_to_string("/conformance-isolation")
                        .unwrap_or_else(|_| "missing".to_string()),
                    _ => format!(
                        "minimal environment received {}: {}",
                        request.tool_name, request.arguments_json
                    ),
                };
                pb::ExecuteResponse {
                    content,
                    is_error: false,
                }
                .encode_to_vec()
            }
            1 => {
                let _ = pb::EvaluateRequest::decode(payload.as_slice())
                    .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
                pb::EvaluateResponse {
                    reward: 1.0,
                    detail_json: r#"{"environment":"minimal"}"#.into(),
                    infra_error: false,
                }
                .encode_to_vec()
            }
            _ => return Err(io::Error::new(io::ErrorKind::InvalidData, "unknown kind")),
        };
        write_frame(&mut stream, &response)?;
    }
}

#[cfg(target_os = "linux")]
fn main() -> io::Result<()> {
    for required in ["/run/grl/task/fixture", "/run/grl/environment/entrypoint"] {
        if !std::path::Path::new(required).exists() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("required conformance path is missing: {required}"),
            ));
        }
    }
    let mut master = 0;
    let mut slave = 0;
    if unsafe {
        libc::openpty(
            &mut master,
            &mut slave,
            std::ptr::null_mut(),
            std::ptr::null(),
            std::ptr::null(),
        )
    } != 0
    {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("conformance openpty failed: {}", io::Error::last_os_error()),
        ));
    }
    unsafe {
        libc::close(master);
        libc::close(slave);
    }
    let listener = vsock::VsockListener::bind_with_cid_port(vsock::VMADDR_CID_ANY, 5005)?;
    for stream in listener.incoming() {
        handle(stream?)?;
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("grl-minimal-env is Linux-only");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    struct MemoryStream {
        input: Cursor<Vec<u8>>,
        output: Vec<u8>,
    }

    impl Read for MemoryStream {
        fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
            self.input.read(buffer)
        }
    }

    impl Write for MemoryStream {
        fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
            self.output.extend_from_slice(buffer);
            Ok(buffer.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn evaluates_without_any_swebench_state() {
        let request = pb::EvaluateRequest {
            env_id: "example".into(),
        }
        .encode_to_vec();
        let mut input = vec![1];
        input.extend_from_slice(&(request.len() as u32).to_be_bytes());
        input.extend_from_slice(&request);
        let mut stream = MemoryStream {
            input: Cursor::new(input),
            output: Vec::new(),
        };
        handle(&mut stream).unwrap();
        let response = pb::EvaluateResponse::decode(&stream.output[4..]).unwrap();
        assert_eq!(response.reward, 1.0);
        assert!(!response.infra_error);
    }
}
