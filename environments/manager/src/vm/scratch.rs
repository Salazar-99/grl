//! Per-VM scratch disk: sparse/reflink copy of the node-local template.

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

#[cfg(unix)]
use std::os::unix::io::AsRawFd;

/// Copy `src` → `dst`, preserving file holes (and using CoW reflink when the
/// filesystem allows it).
///
/// `std::fs::copy` / `copy_file_range` densify sparse files across mounts
/// (e.g. XFS hostPath template → overlay run dir), writing every logical zero
/// byte. Concurrent densifying copies of multi-GB scratch templates saturate
/// dirty-page writeback and stall boots. This path only materializes extents
/// that contain data.
#[cfg(test)]
pub fn copy_scratch_template(src: &Path, dst: &Path) -> io::Result<()> {
    let cancelled = AtomicBool::new(false);
    copy_scratch_template_cancelable(src, dst, &cancelled)
}

/// Cancellation-aware variant used by VM boot tasks.
///
/// A blocking filesystem operation cannot be force-cancelled safely, but the
/// sparse-copy fallback checks this flag between each MiB chunk. Teardown can
/// therefore bound detached writeback instead of leaving a multi-GB copy alive.
pub fn copy_scratch_template_cancelable(
    src: &Path,
    dst: &Path,
    cancelled: &AtomicBool,
) -> io::Result<()> {
    let result = copy_sparse(src, dst, cancelled);
    if result.is_err() {
        let _ = std::fs::remove_file(dst);
    }
    result
}

fn copy_sparse(src: &Path, dst: &Path, cancelled: &AtomicBool) -> io::Result<()> {
    check_cancelled(cancelled)?;
    let mut src_file = File::open(src)?;
    let len = src_file.metadata()?.len();

    let mut dst_file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(dst)?;

    #[cfg(target_os = "linux")]
    if try_ficlone(&src_file, &dst_file)? {
        check_cancelled(cancelled)?;
        return Ok(());
    }

    dst_file.set_len(len)?;

    #[cfg(unix)]
    {
        copy_extents(&mut src_file, &mut dst_file, len, cancelled)?;
    }
    #[cfg(not(unix))]
    {
        let _ = len;
        let _ = cancelled;
        std::io::copy(&mut src_file, &mut dst_file)?;
    }

    check_cancelled(cancelled)?;
    dst_file.sync_all()?;
    Ok(())
}

/// Instant CoW clone when `src` and `dst` share a reflink-capable filesystem.
/// Returns `Ok(true)` on success, `Ok(false)` when the ioctl is unsupported /
/// cross-device so the caller can fall back to a sparse extent copy.
#[cfg(target_os = "linux")]
fn try_ficlone(src: &File, dst: &File) -> io::Result<bool> {
    // FICLONE: _IOW(0x94, 9, int)
    const FICLONE: libc::c_ulong = 0x4004_9409;
    let rc = unsafe { libc::ioctl(dst.as_raw_fd(), FICLONE, src.as_raw_fd()) };
    if rc == 0 {
        return Ok(true);
    }
    let err = io::Error::last_os_error();
    match err.raw_os_error() {
        // Cross-device, unsupported FS, or dest not empty-friendly — fall back.
        Some(libc::EXDEV | libc::EOPNOTSUPP | libc::EINVAL | libc::ENOTTY) => Ok(false),
        _ => Err(err),
    }
}

#[cfg(unix)]
fn copy_extents(
    src: &mut File,
    dst: &mut File,
    len: u64,
    cancelled: &AtomicBool,
) -> io::Result<()> {
    // Linux: SEEK_DATA=3, SEEK_HOLE=4. BSD/macOS swap those values.
    #[cfg(target_os = "linux")]
    const SEEK_DATA: i32 = 3;
    #[cfg(target_os = "linux")]
    const SEEK_HOLE: i32 = 4;
    #[cfg(not(target_os = "linux"))]
    const SEEK_DATA: i32 = 4;
    #[cfg(not(target_os = "linux"))]
    const SEEK_HOLE: i32 = 3;

    let mut offset: u64 = 0;
    let mut buf = vec![0u8; 1024 * 1024];

    while offset < len {
        check_cancelled(cancelled)?;
        let data = match lseek_whence(src, offset, SEEK_DATA) {
            Ok(off) => off,
            // No more data extents — trailing hole already covered by set_len.
            Err(e) if e.raw_os_error() == Some(libc::ENXIO) => break,
            Err(e) => return Err(e),
        };
        if data >= len {
            break;
        }
        let hole = match lseek_whence(src, data, SEEK_HOLE) {
            Ok(off) => off.min(len),
            Err(_) => len,
        };

        let mut pos = data;
        src.seek(SeekFrom::Start(data))?;
        dst.seek(SeekFrom::Start(data))?;
        while pos < hole {
            check_cancelled(cancelled)?;
            let chunk = ((hole - pos) as usize).min(buf.len());
            src.read_exact(&mut buf[..chunk])?;
            dst.write_all(&buf[..chunk])?;
            pos += chunk as u64;
        }
        offset = hole;
    }
    Ok(())
}

fn check_cancelled(cancelled: &AtomicBool) -> io::Result<()> {
    if cancelled.load(Ordering::Relaxed) {
        Err(io::Error::new(
            io::ErrorKind::Interrupted,
            "scratch copy cancelled",
        ))
    } else {
        Ok(())
    }
}

#[cfg(unix)]
fn lseek_whence(file: &File, offset: u64, whence: i32) -> io::Result<u64> {
    let off = unsafe { libc::lseek(file.as_raw_fd(), offset as libc::off_t, whence) };
    if off < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(off as u64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io::Write;

    #[cfg(unix)]
    use std::os::unix::fs::MetadataExt;

    #[test]
    fn sparse_copy_preserves_holes() {
        let dir = std::env::temp_dir().join(format!(
            "grl-scratch-sparse-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();

        let src = dir.join("template.ext4");
        let dst = dir.join("scratch.ext4");

        // 32 MiB logical file with a small header — mostly holes.
        let logical: u64 = 32 << 20;
        {
            let mut f = File::create(&src).unwrap();
            f.write_all(b"grl-scratch-hdr").unwrap();
            f.set_len(logical).unwrap();
            f.sync_all().unwrap();
        }

        copy_scratch_template(&src, &dst).unwrap();

        let src_meta = fs::metadata(&src).unwrap();
        let dst_meta = fs::metadata(&dst).unwrap();
        assert_eq!(dst_meta.len(), logical);
        assert_eq!(dst_meta.len(), src_meta.len());

        #[cfg(unix)]
        {
            // Allocated bytes should stay far below the logical size (exact
            // block count varies by FS; require < 1 MiB allocated).
            let allocated = dst_meta.blocks() * 512;
            assert!(
                allocated < 1 << 20,
                "expected sparse dst, allocated {allocated} bytes for {logical} logical"
            );
        }

        let mut got = vec![0u8; 15];
        File::open(&dst).unwrap().read_exact(&mut got).unwrap();
        assert_eq!(&got, b"grl-scratch-hdr");

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn cancelled_copy_stops_and_removes_destination() {
        let dir = std::env::temp_dir().join(format!("grl-scratch-cancel-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let src = dir.join("template.ext4");
        let dst = dir.join("scratch.ext4");
        fs::write(&src, b"template").unwrap();
        let cancelled = AtomicBool::new(true);

        let err = copy_scratch_template_cancelable(&src, &dst, &cancelled).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::Interrupted);
        assert!(!dst.exists());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn restored_scratch_clones_are_write_isolated() {
        let dir =
            std::env::temp_dir().join(format!("grl-scratch-isolation-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let prepared = dir.join("prepared.ext4");
        let first = dir.join("first.ext4");
        let second = dir.join("second.ext4");
        fs::write(&prepared, b"golden").unwrap();
        copy_scratch_template(&prepared, &first).unwrap();
        copy_scratch_template(&prepared, &second).unwrap();

        fs::write(&first, b"rollout-one").unwrap();
        assert_eq!(fs::read(&second).unwrap(), b"golden");
        assert_eq!(fs::read(&prepared).unwrap(), b"golden");
        let _ = fs::remove_dir_all(&dir);
    }
}
