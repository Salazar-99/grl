//! Generic initramfs PID 1 for managed GRL Firecracker guests.

#[cfg(target_os = "linux")]
mod linux {
    use std::ffi::CString;
    use std::fs;
    use std::io;
    use std::os::unix::fs::PermissionsExt;
    use std::path::Path;
    use std::process;
    use std::sync::atomic::{AtomicI32, Ordering};
    use std::thread;
    use std::time::Duration;

    static CHILD: AtomicI32 = AtomicI32::new(0);

    fn cstring(value: &str) -> io::Result<CString> {
        CString::new(value)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "path contains NUL"))
    }

    fn mkdir(path: &str) -> io::Result<()> {
        fs::create_dir_all(path)
    }

    fn mount(
        source: &str,
        target: &str,
        fstype: &str,
        flags: libc::c_ulong,
        data: Option<&str>,
    ) -> io::Result<()> {
        let source = cstring(source)?;
        let target = cstring(target)?;
        let fstype = cstring(fstype)?;
        let data = data.map(cstring).transpose()?;
        let result = unsafe {
            libc::mount(
                source.as_ptr(),
                target.as_ptr(),
                fstype.as_ptr(),
                flags,
                data.as_ref()
                    .map_or(std::ptr::null(), |value| value.as_ptr().cast()),
            )
        };
        if result == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    fn move_mount(source: &str, target: &str) -> io::Result<()> {
        mount(source, target, "", libc::MS_MOVE, None)
    }

    fn wait_for(path: &str) -> io::Result<()> {
        for _ in 0..200 {
            if Path::new(path).exists() {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(25));
        }
        Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("block device {path} did not appear"),
        ))
    }

    fn pivot_root(new_root: &str, put_old: &str) -> io::Result<()> {
        let new_root = cstring(new_root)?;
        let put_old = cstring(put_old)?;
        let result =
            unsafe { libc::syscall(libc::SYS_pivot_root, new_root.as_ptr(), put_old.as_ptr()) };
        if result == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    fn exec(path: &str, args: &[&str]) -> io::Result<()> {
        let path = cstring(path)?;
        let values: Vec<CString> = args
            .iter()
            .map(|value| cstring(value))
            .collect::<io::Result<_>>()?;
        let mut pointers: Vec<*const libc::c_char> =
            values.iter().map(|value| value.as_ptr()).collect();
        pointers.push(std::ptr::null());
        unsafe {
            libc::execv(path.as_ptr(), pointers.as_ptr());
        }
        Err(io::Error::last_os_error())
    }

    fn prepare_root() -> io::Result<()> {
        for path in [
            "/proc", "/sys", "/dev", "/dev/pts", "/lower", "/scratch", "/newroot",
        ] {
            mkdir(path)?;
        }
        mount("proc", "/proc", "proc", 0, None)?;
        mount("sysfs", "/sys", "sysfs", 0, None)?;
        mount("devtmpfs", "/dev", "devtmpfs", 0, None)?;
        mkdir("/dev/pts")?;
        mount(
            "devpts",
            "/dev/pts",
            "devpts",
            0,
            Some("newinstance,ptmxmode=0666,mode=0620,gid=5"),
        )?;
        let _ = fs::remove_file("/dev/ptmx");
        std::os::unix::fs::symlink("pts/ptmx", "/dev/ptmx")?;

        for device in ["/dev/vda", "/dev/vdb", "/dev/vdc", "/dev/vdd"] {
            wait_for(device)?;
        }

        mount("/dev/vda", "/lower", "squashfs", libc::MS_RDONLY, None)?;

        // With an environment package, vdc is its squashfs and vdd is scratch.
        // The generic bootstrap intentionally requires that final drive layout.
        if !Path::new("/dev/vdd").exists() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "generic bootstrap requires environment drive vdc and scratch drive vdd",
            ));
        }
        mount("/dev/vdd", "/scratch", "ext4", 0, None)?;
        mkdir("/scratch/root/upper")?;
        mkdir("/scratch/root/work")?;
        mount(
            "overlay",
            "/newroot",
            "overlay",
            0,
            Some("lowerdir=/lower,upperdir=/scratch/root/upper,workdir=/scratch/root/work"),
        )?;

        mkdir("/newroot/usr/local/bin")?;
        fs::copy("/proc/self/exe", "/newroot/usr/local/bin/grl-bootstrap")?;
        fs::set_permissions(
            "/newroot/usr/local/bin/grl-bootstrap",
            fs::Permissions::from_mode(0o755),
        )?;

        let root = cstring("/")?;
        let result = unsafe {
            libc::mount(
                std::ptr::null(),
                root.as_ptr(),
                std::ptr::null(),
                libc::MS_PRIVATE | libc::MS_REC,
                std::ptr::null(),
            )
        };
        if result != 0 {
            return Err(io::Error::last_os_error());
        }
        mkdir("/newroot/oldroot")?;
        for name in ["proc", "sys", "dev", "scratch"] {
            let target = format!("/newroot/{name}");
            mkdir(&target)?;
            move_mount(&format!("/{name}"), &target)?;
        }
        std::env::set_current_dir("/newroot")?;
        pivot_root(".", "oldroot")?;

        mkdir("/run/grl/task")?;
        mkdir("/run/grl/environment")?;
        mount(
            "/dev/vdb",
            "/run/grl/task",
            "squashfs",
            libc::MS_RDONLY,
            None,
        )?;
        mount(
            "/dev/vdc",
            "/run/grl/environment",
            "squashfs",
            libc::MS_RDONLY,
            None,
        )?;
        exec(
            "/usr/local/bin/grl-bootstrap",
            &["grl-bootstrap", "--supervise"],
        )
    }

    extern "C" fn forward_signal(signal: libc::c_int) {
        let child = CHILD.load(Ordering::Relaxed);
        if child > 0 {
            unsafe {
                libc::kill(-child, signal);
            }
        }
    }

    fn supervise() -> io::Result<()> {
        let mut blocked = unsafe { std::mem::zeroed::<libc::sigset_t>() };
        let mut previous = unsafe { std::mem::zeroed::<libc::sigset_t>() };
        unsafe {
            libc::sigemptyset(&mut blocked);
            libc::sigaddset(&mut blocked, libc::SIGTERM);
            libc::sigaddset(&mut blocked, libc::SIGINT);
            if libc::sigprocmask(libc::SIG_BLOCK, &blocked, &mut previous) != 0 {
                return Err(io::Error::last_os_error());
            }
            libc::signal(
                libc::SIGTERM,
                forward_signal as *const () as libc::sighandler_t,
            );
            libc::signal(
                libc::SIGINT,
                forward_signal as *const () as libc::sighandler_t,
            );
        }
        let child = unsafe { libc::fork() };
        if child < 0 {
            return Err(io::Error::last_os_error());
        }
        if child == 0 {
            unsafe {
                libc::setpgid(0, 0);
                libc::signal(libc::SIGTERM, libc::SIG_DFL);
                libc::signal(libc::SIGINT, libc::SIG_DFL);
                libc::sigprocmask(libc::SIG_SETMASK, &previous, std::ptr::null_mut());
            }
            let _ = exec(
                "/run/grl/environment/entrypoint",
                &["/run/grl/environment/entrypoint"],
            );
            unsafe { libc::_exit(127) };
        }
        unsafe {
            libc::setpgid(child, child);
        }
        CHILD.store(child, Ordering::Relaxed);
        unsafe {
            if libc::sigprocmask(libc::SIG_SETMASK, &previous, std::ptr::null_mut()) != 0 {
                return Err(io::Error::last_os_error());
            }
        }

        let mut main_status = 1;
        loop {
            let mut status = 0;
            let reaped = unsafe { libc::waitpid(-1, &mut status, 0) };
            if reaped < 0 {
                let error = io::Error::last_os_error();
                if error.kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                break;
            }
            if reaped == child {
                main_status = if libc::WIFEXITED(status) {
                    libc::WEXITSTATUS(status)
                } else {
                    128 + libc::WTERMSIG(status)
                };
                break;
            }
        }
        unsafe {
            libc::kill(-child, libc::SIGTERM);
        }
        process::exit(main_status)
    }

    pub fn run() -> io::Result<()> {
        if std::env::args().any(|arg| arg == "--supervise") {
            supervise()
        } else {
            prepare_root()
        }
    }
}

fn main() {
    #[cfg(target_os = "linux")]
    if let Err(error) = linux::run() {
        eprintln!("grl-bootstrap: {error}");
        std::process::exit(1);
    }
    #[cfg(not(target_os = "linux"))]
    {
        eprintln!("grl-bootstrap is Linux-only");
        std::process::exit(1);
    }
}
