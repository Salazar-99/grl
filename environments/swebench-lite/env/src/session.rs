//! Persistent per-environment shells.
//!
//! Each environment owns one long-lived shell so state — cwd, exported vars,
//! `source .venv/bin/activate`, shell functions, background jobs — persists
//! across tool calls. The shell runs under a PTY so programs that probe
//! `isatty()` behave (color, pagers) and so we can deliver signals (Ctrl-C) to
//! a runaway command.
//!
//! The hard part of a persistent shell is knowing when a command is done: there
//! is no per-command process to wait on. We frame each command with a unique
//! sentinel that also carries the exit code, then read the PTY until it appears.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError, Sender};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize};

/// Marker prefix emitted after every command. The full sentinel is
/// `__GRL_DONE_<id>__<exit_code>\n`, where `<id>` is unique per command so a
/// command that happens to print this string can't be mistaken for the fence.
const SENTINEL_PREFIX: &str = "__GRL_DONE_";

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

/// Result of running one command in a session.
pub struct CommandOutput {
    pub content: String,
    pub exit_code: i32,
}

/// A single persistent shell behind a PTY.
///
/// The PTY master's blocking reader is drained on a dedicated thread into a
/// channel; `run` pulls from the channel with a deadline so a hung command
/// can't wedge the executor.
pub struct Session {
    writer: Box<dyn Write + Send>,
    rx: Receiver<Vec<u8>>,
    child: Box<dyn Child + Send + Sync>,
    // Keep the master alive for the life of the session: dropping it closes the
    // PTY and kills the shell. The writer above is taken from it.
    _master: Box<dyn MasterPty + Send>,
}

impl Session {
    /// Spawn a fresh login-ish shell and bring it into a known, quiet state:
    /// no command echo, empty prompts. After this returns the session is ready
    /// to accept `run`.
    pub fn spawn() -> std::io::Result<Session> {
        let pair = native_pty_system()
            .openpty(PtySize {
                rows: 24,
                cols: 120,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(to_io)?;

        // --norc/--noprofile so behavior doesn't drift with image-baked dotfiles.
        let mut cmd = CommandBuilder::new("bash");
        cmd.args(["--norc", "--noprofile", "-i"]);
        cmd.env("TERM", "xterm-256color");
        let child = pair.slave.spawn_command(cmd).map_err(to_io)?;
        // The child holds its own handle to the slave; we don't need ours.
        drop(pair.slave);

        let writer = pair.master.take_writer().map_err(to_io)?;
        let mut reader = pair.master.try_clone_reader().map_err(to_io)?;

        let (tx, rx): (Sender<Vec<u8>>, Receiver<Vec<u8>>) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let mut buf = [0u8; 8192];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => break, // shell exited, PTY closed
                    Ok(n) => {
                        if tx.send(buf[..n].to_vec()).is_err() {
                            break; // Session dropped
                        }
                    }
                    Err(_) => break,
                }
            }
        });

        let mut session = Session {
            writer,
            rx,
            child,
            _master: pair.master,
        };

        // Quiet the terminal: turn off input echo so our command lines don't
        // bounce back into the output stream, and blank the prompts so PS1/PS2
        // never appear between commands. This first `run` also synchronizes us
        // with the shell — we don't return until its sentinel comes back.
        session.run(
            "stty -echo; export PS1='' PS2='' PROMPT_COMMAND=''",
            Duration::from_secs(10),
        )?;

        Ok(session)
    }

    /// Run one command to completion, returning its stdout+stderr (interleaved,
    /// as a TTY delivers them) and exit code.
    ///
    /// `timeout` bounds the whole call: on expiry we send Ctrl-C and return an
    /// error rather than blocking forever on a command that reads stdin or hangs.
    pub fn run(&mut self, command: &str, timeout: Duration) -> std::io::Result<CommandOutput> {
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let marker = format!("{SENTINEL_PREFIX}{id}__");

        // Send the command, then a printf that prints the sentinel with `$?`.
        // The leading \n guarantees the sentinel starts on its own line even if
        // the command left the cursor mid-line with no trailing newline.
        write!(
            self.writer,
            "{command}\nprintf '\\n{marker}%d\\n' \"$?\"\n"
        )?;
        self.writer.flush()?;

        let needle = marker.as_bytes();
        let deadline = Instant::now() + timeout;
        let mut buf: Vec<u8> = Vec::new();

        loop {
            if let Some((end, code)) = find_sentinel(&buf, needle) {
                // Strip the single '\n' we prepended in printf.
                let mut content_end = end;
                if content_end > 0 && buf[content_end - 1] == b'\n' {
                    content_end -= 1;
                }
                let content = String::from_utf8_lossy(&buf[..content_end]).into_owned();
                return Ok(CommandOutput {
                    content,
                    exit_code: code,
                });
            }

            let now = Instant::now();
            if now >= deadline {
                self.interrupt()?; // try to unstick the shell for the next call
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    format!("command timed out after {timeout:?}"),
                ));
            }

            match self.rx.recv_timeout(deadline - now) {
                Ok(chunk) => buf.extend_from_slice(&chunk),
                Err(RecvTimeoutError::Timeout) => continue, // loop re-checks deadline
                Err(RecvTimeoutError::Disconnected) => {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::BrokenPipe,
                        "shell exited unexpectedly",
                    ));
                }
            }
        }
    }

    /// Send Ctrl-C to the foreground process group of the shell's TTY.
    pub fn interrupt(&mut self) -> std::io::Result<()> {
        self.writer.write_all(&[0x03])?;
        self.writer.flush()
    }

    /// Terminate the shell and free the PTY.
    pub fn close(mut self) {
        let _ = self.child.kill();
    }
}

/// Locate a complete sentinel in `buf`: the marker immediately followed by an
/// integer exit code and a newline. Returns the byte offset where the sentinel
/// begins (i.e. the end of command output) and the parsed exit code.
///
/// Crucially, candidates not followed by a valid `<int>\n` are skipped. That's
/// what lets us ignore the literal `...__%d` text if echo were ever on, and any
/// occurrence still awaiting its trailing newline.
fn find_sentinel(buf: &[u8], marker: &[u8]) -> Option<(usize, i32)> {
    let mut start = 0;
    while let Some(rel) = find_subslice(&buf[start..], marker) {
        let pos = start + rel;
        let after = pos + marker.len();
        match buf[after..].iter().position(|&b| b == b'\n') {
            Some(nl) => {
                if let Ok(s) = std::str::from_utf8(&buf[after..after + nl]) {
                    if let Ok(code) = s.trim().parse::<i32>() {
                        return Some((pos, code));
                    }
                }
                // Bad number (e.g. echoed "%d") — keep scanning after it.
                start = after;
            }
            // Marker present but its code/newline hasn't arrived yet: this is
            // the earliest occurrence, so no later one can be complete. Wait.
            None => return None,
        }
    }
    None
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || haystack.len() < needle.len() {
        return None;
    }
    haystack.windows(needle.len()).position(|w| w == needle)
}

/// Per-executor registry of live sessions, keyed by `env_id`. In the common
/// one-VM-per-env deployment this holds a single entry, but keying by `env_id`
/// keeps the door open for multiplexing and matches the gRPC contract.
#[derive(Default)]
pub struct Sessions {
    inner: Mutex<HashMap<String, Session>>,
}

impl Sessions {
    /// Run `command` in the session for `env_id`, spawning the shell on first
    /// use. (In the one-VM-per-env model the manager boots a VM per env, so the
    /// first forwarded tool call lazily brings the shell up.)
    pub fn execute(
        &self,
        env_id: &str,
        command: &str,
        timeout: Duration,
    ) -> std::io::Result<CommandOutput> {
        let mut guard = self.inner.lock().unwrap();
        if !guard.contains_key(env_id) {
            guard.insert(env_id.to_string(), Session::spawn()?);
        }
        // Present unconditionally: just inserted or already there.
        guard.get_mut(env_id).unwrap().run(command, timeout)
    }

    pub fn close(&self, env_id: &str) {
        if let Some(session) = self.inner.lock().unwrap().remove(env_id) {
            session.close();
        }
    }
}

fn to_io<E: std::fmt::Display>(e: E) -> std::io::Error {
    std::io::Error::other(e.to_string())
}
