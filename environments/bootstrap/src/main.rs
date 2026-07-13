//! Generic initramfs PID 1 for managed GRL Firecracker guests.

#[cfg(target_os = "linux")]
mod linux {
    use std::ffi::CString;
    use std::fs;
    use std::io;
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

    fn bind_mount(source: &str, target: &str) -> io::Result<()> {
        mount(source, target, "", libc::MS_BIND | libc::MS_REC, None)
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

    fn chroot(path: &str) -> io::Result<()> {
        let path = cstring(path)?;
        let result = unsafe { libc::chroot(path.as_ptr()) };
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

    fn stage<T>(name: &str, result: io::Result<T>) -> io::Result<T> {
        result.map_err(|error| io::Error::new(error.kind(), format!("{name}: {error}")))
    }

    fn prepare_sandbox() -> io::Result<()> {
        for path in [
            "/proc",
            "/sys",
            "/dev",
            "/dev/pts",
            "/lower",
            "/scratch",
            "/sandbox-root",
        ] {
            stage(&format!("create {path}"), mkdir(path))?;
        }
        stage("mount proc", mount("proc", "/proc", "proc", 0, None))?;
        stage("mount sysfs", mount("sysfs", "/sys", "sysfs", 0, None))?;
        stage(
            "mount devtmpfs",
            mount("devtmpfs", "/dev", "devtmpfs", 0, None),
        )?;
        stage("create /dev/pts", mkdir("/dev/pts"))?;
        stage(
            "mount devpts",
            mount(
                "devpts",
                "/dev/pts",
                "devpts",
                0,
                Some("newinstance,ptmxmode=0666,mode=0620,gid=5"),
            ),
        )?;
        let _ = fs::remove_file("/dev/ptmx");
        stage(
            "link /dev/ptmx",
            std::os::unix::fs::symlink("pts/ptmx", "/dev/ptmx"),
        )?;

        for device in ["/dev/vda", "/dev/vdb", "/dev/vdc", "/dev/vdd"] {
            stage(&format!("wait for {device}"), wait_for(device))?;
        }

        stage(
            "mount base squashfs",
            mount("/dev/vda", "/lower", "squashfs", libc::MS_RDONLY, None),
        )?;
        stage(
            "mount scratch ext4",
            mount("/dev/vdd", "/scratch", "ext4", 0, None),
        )?;
        stage("create overlay upper", mkdir("/scratch/root/upper"))?;
        stage("create overlay work", mkdir("/scratch/root/work"))?;
        stage(
            "mount workload overlay",
            mount(
                "overlay",
                "/sandbox-root",
                "overlay",
                0,
                Some("lowerdir=/lower,upperdir=/scratch/root/upper,workdir=/scratch/root/work"),
            ),
        )?;

        for name in ["proc", "sys", "dev"] {
            let target = format!("/sandbox-root/{name}");
            stage(&format!("create {target}"), mkdir(&target))?;
            stage(
                &format!("bind /{name} into sandbox"),
                bind_mount(&format!("/{name}"), &target),
            )?;
        }

        stage("create task mount", mkdir("/sandbox-root/run/grl/task"))?;
        stage(
            "create environment mount",
            mkdir("/sandbox-root/run/grl/environment"),
        )?;
        stage(
            "mount task squashfs",
            mount(
                "/dev/vdb",
                "/sandbox-root/run/grl/task",
                "squashfs",
                libc::MS_RDONLY,
                None,
            ),
        )?;
        stage(
            "mount environment squashfs",
            mount(
                "/dev/vdc",
                "/sandbox-root/run/grl/environment",
                "squashfs",
                libc::MS_RDONLY,
                None,
            ),
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

    fn status_code(status: libc::c_int) -> libc::c_int {
        if libc::WIFEXITED(status) {
            libc::WEXITSTATUS(status)
        } else {
            128 + libc::WTERMSIG(status)
        }
    }

    fn reap_remaining(process_group: libc::pid_t) {
        unsafe {
            libc::kill(-process_group, libc::SIGTERM);
        }
        for _ in 0..50 {
            let mut reaped_any = false;
            loop {
                let mut status = 0;
                let reaped = unsafe { libc::waitpid(-1, &mut status, libc::WNOHANG) };
                if reaped > 0 {
                    reaped_any = true;
                    continue;
                }
                if reaped == 0 {
                    break;
                }
                let error = io::Error::last_os_error();
                if error.raw_os_error() == Some(libc::ECHILD) {
                    return;
                }
                if error.kind() != io::ErrorKind::Interrupted {
                    break;
                }
            }
            if !reaped_any {
                thread::sleep(Duration::from_millis(10));
            }
        }
        unsafe {
            libc::kill(-process_group, libc::SIGKILL);
        }
        loop {
            let mut status = 0;
            let reaped = unsafe { libc::waitpid(-1, &mut status, 0) };
            if reaped > 0 {
                continue;
            }
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            break;
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
            if unsafe { libc::unshare(libc::CLONE_NEWNS) } != 0 {
                eprintln!(
                    "grl-bootstrap: create workload mount namespace: {}",
                    io::Error::last_os_error()
                );
                unsafe { libc::_exit(127) };
            }
            if let Err(error) = mount("", "/", "", libc::MS_PRIVATE | libc::MS_REC, None) {
                eprintln!("grl-bootstrap: make workload mounts private: {error}");
                unsafe { libc::_exit(127) };
            }
            if let Err(error) = chroot("/sandbox-root") {
                eprintln!("grl-bootstrap: chroot workload: {error}");
                unsafe { libc::_exit(127) };
            }
            if let Err(error) = std::env::set_current_dir("/") {
                eprintln!("grl-bootstrap: enter workload root: {error}");
                unsafe { libc::_exit(127) };
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
                main_status = status_code(status);
                break;
            }
        }
        reap_remaining(child);
        CHILD.store(0, Ordering::Relaxed);
        process::exit(main_status)
    }

    pub fn run() -> io::Result<()> {
        prepare_sandbox()?;
        supervise()
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
