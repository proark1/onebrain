#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/landlock.h>
#include <seccomp.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef LANDLOCK_ACCESS_FS_REFER
#define LANDLOCK_ACCESS_FS_REFER (1ULL << 13)
#endif
#ifndef LANDLOCK_ACCESS_FS_TRUNCATE
#define LANDLOCK_ACCESS_FS_TRUNCATE (1ULL << 14)
#endif

#define EXIT_SANDBOX_FAILURE 125
#define MAX_CHILD_ARGS 32
#define DEFINITION_ROOT "/var/lib/onebrain/clamav"
#define DEFINITION_SETS_ROOT DEFINITION_ROOT "/sets"
#define DEFINITION_INCOMING_ROOT DEFINITION_ROOT "/incoming"
#define SCAN_TEMP_ROOT "/tmp/onebrain-scanner"
#define CLAMSCAN_BINARY "/usr/bin/clamscan"
#define FRESHCLAM_BINARY "/usr/bin/freshclam"
#define FRESHCLAM_CONFIG "/etc/onebrain/freshclam.conf"

extern char **environ;

static int landlock_create_ruleset(const struct landlock_ruleset_attr *attr,
                                   size_t size, uint32_t flags) {
    return (int)syscall(__NR_landlock_create_ruleset, attr, size, flags);
}

static int landlock_add_rule(int ruleset_fd, enum landlock_rule_type type,
                             const void *attr, uint32_t flags) {
    return (int)syscall(__NR_landlock_add_rule, ruleset_fd, type, attr, flags);
}

static int landlock_restrict_self(int ruleset_fd, uint32_t flags) {
    return (int)syscall(__NR_landlock_restrict_self, ruleset_fd, flags);
}

static void fail(const char *message) {
    fprintf(stderr, "onebrain scanner sandbox: %s\n", message);
    _exit(EXIT_SANDBOX_FAILURE);
}

static bool starts_with_path(const char *path, const char *root) {
    size_t root_len = strlen(root);
    return strncmp(path, root, root_len) == 0 &&
           (path[root_len] == '\0' || path[root_len] == '/');
}

static bool all_decimal(const char *value) {
    if (value == NULL || *value == '\0') return false;
    for (const char *cursor = value; *cursor != '\0'; cursor++) {
        if (*cursor < '0' || *cursor > '9') return false;
    }
    return true;
}

static bool numeric_option(const char *arg, const char *prefix, unsigned long long maximum) {
    size_t prefix_len = strlen(prefix);
    if (strncmp(arg, prefix, prefix_len) != 0 || !all_decimal(arg + prefix_len)) return false;
    errno = 0;
    unsigned long long value = strtoull(arg + prefix_len, NULL, 10);
    return errno == 0 && value > 0 && value <= maximum;
}

static bool valid_scan_argument(const char *arg) {
    static const char *fixed[] = {
        "--stdout", "--no-summary", "--infected", "--scan-archive=yes",
        "--alert-exceeds-max=yes", "--alert-encrypted=yes", "--official-db-only=yes",
        "--version", "-"
    };
    for (size_t i = 0; i < sizeof(fixed) / sizeof(fixed[0]); i++) {
        if (strcmp(arg, fixed[i]) == 0) return true;
    }
    if (numeric_option(arg, "--max-scansize=", 512ULL * 1024 * 1024)) return true;
    if (numeric_option(arg, "--max-filesize=", 100ULL * 1024 * 1024)) return true;
    if (numeric_option(arg, "--max-files=", 10000)) return true;
    if (numeric_option(arg, "--max-recursion=", 16)) return true;
    if (numeric_option(arg, "--max-scantime=", 90000)) return true;
    if (numeric_option(arg, "--bytecode-timeout=", 30000)) return true;
    if (strncmp(arg, "--database=", 11) == 0) {
        char resolved[4096];
        if (realpath(arg + 11, resolved) == NULL) return false;
        return starts_with_path(resolved, DEFINITION_SETS_ROOT);
    }
    return false;
}

static void set_limit(int resource, rlim_t soft, rlim_t hard) {
    struct rlimit value = {.rlim_cur = soft, .rlim_max = hard};
    if (setrlimit(resource, &value) != 0) fail("could not apply resource limits");
}

static void apply_resource_limits(bool update_profile) {
    set_limit(RLIMIT_CORE, 0, 0);
    set_limit(RLIMIT_NOFILE, 32, 32);
    set_limit(RLIMIT_NPROC, 32, 32);
    set_limit(RLIMIT_FSIZE,
              update_profile ? (rlim_t)1024 * 1024 * 1024 : (rlim_t)512 * 1024 * 1024,
              update_profile ? (rlim_t)1024 * 1024 * 1024 : (rlim_t)512 * 1024 * 1024);
    set_limit(RLIMIT_AS, (rlim_t)2 * 1024 * 1024 * 1024,
              (rlim_t)2 * 1024 * 1024 * 1024);
    set_limit(RLIMIT_CPU, update_profile ? 900 : 90, update_profile ? 900 : 90);
}

static void deny_syscall(scmp_filter_ctx context, int syscall_number) {
    if (syscall_number == __NR_SCMP_ERROR) return;
    if (seccomp_rule_add(context, SCMP_ACT_ERRNO(EPERM), syscall_number, 0) != 0)
        fail("could not install seccomp rule");
}

static void apply_seccomp(bool update_profile) {
    scmp_filter_ctx context = seccomp_init(SCMP_ACT_ALLOW);
    if (context == NULL) fail("could not create seccomp filter");

    int always_denied[] = {
        SCMP_SYS(ptrace), SCMP_SYS(process_vm_readv), SCMP_SYS(process_vm_writev),
        SCMP_SYS(mount), SCMP_SYS(umount2), SCMP_SYS(pivot_root),
        SCMP_SYS(open_by_handle_at), SCMP_SYS(bpf), SCMP_SYS(perf_event_open),
        SCMP_SYS(keyctl), SCMP_SYS(add_key), SCMP_SYS(request_key),
        SCMP_SYS(kexec_load), SCMP_SYS(init_module), SCMP_SYS(finit_module),
        SCMP_SYS(delete_module), SCMP_SYS(userfaultfd), SCMP_SYS(unshare),
        SCMP_SYS(setns), SCMP_SYS(io_uring_setup), SCMP_SYS(io_uring_enter),
        SCMP_SYS(io_uring_register)
    };
    for (size_t i = 0; i < sizeof(always_denied) / sizeof(always_denied[0]); i++)
        deny_syscall(context, always_denied[i]);

    if (!update_profile) {
        int network_denied[] = {
            SCMP_SYS(socket), SCMP_SYS(socketpair), SCMP_SYS(connect), SCMP_SYS(bind),
            SCMP_SYS(listen), SCMP_SYS(accept), SCMP_SYS(accept4),
            SCMP_SYS(sendto), SCMP_SYS(sendmsg), SCMP_SYS(recvfrom), SCMP_SYS(recvmsg)
        };
        for (size_t i = 0; i < sizeof(network_denied) / sizeof(network_denied[0]); i++)
            deny_syscall(context, network_denied[i]);
    }
    if (seccomp_load(context) != 0) {
        seccomp_release(context);
        fail("could not activate seccomp filter");
    }
    seccomp_release(context);
}

static void require_io_uring_call_denied(long result, bool close_result) {
    if (result >= 0) {
        if (close_result) close((int)result);
        fail("sandbox io_uring negative probe unexpectedly succeeded");
    }
    if (errno != EACCES && errno != EPERM && errno != ENOSYS)
        fail("sandbox io_uring negative probe could not prove denial");
}

static void verify_io_uring_denied(void) {
    errno = 0;
    require_io_uring_call_denied(
        syscall(__NR_io_uring_setup, 1U, NULL), true);
    errno = 0;
    require_io_uring_call_denied(
        syscall(__NR_io_uring_enter, -1, 0U, 0U, 0U, NULL, 0U), false);
    errno = 0;
    require_io_uring_call_denied(
        syscall(__NR_io_uring_register, -1, 0U, NULL, 0U), false);
}

static uint64_t read_access(void) {
    return LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE |
           LANDLOCK_ACCESS_FS_READ_DIR;
}

static uint64_t write_access(void) {
    return read_access() | LANDLOCK_ACCESS_FS_WRITE_FILE |
           LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE |
           LANDLOCK_ACCESS_FS_MAKE_CHAR | LANDLOCK_ACCESS_FS_MAKE_DIR |
           LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_SOCK |
           LANDLOCK_ACCESS_FS_MAKE_FIFO | LANDLOCK_ACCESS_FS_MAKE_BLOCK |
           LANDLOCK_ACCESS_FS_MAKE_SYM | LANDLOCK_ACCESS_FS_REFER |
           LANDLOCK_ACCESS_FS_TRUNCATE;
}

static void allow_path(int ruleset_fd, const char *path, uint64_t access) {
    int fd = open(path, O_PATH | O_CLOEXEC);
    if (fd < 0) {
        if (errno == ENOENT) return;
        fail("could not open sandbox allowlist path");
    }
    struct stat metadata;
    if (fstat(fd, &metadata) != 0) {
        close(fd);
        fail("could not inspect sandbox allowlist path");
    }
    if (!S_ISDIR(metadata.st_mode)) {
        access &= LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE |
                  LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_TRUNCATE;
    }
    struct landlock_path_beneath_attr rule = {
        .allowed_access = access,
        .parent_fd = fd,
    };
    if (landlock_add_rule(ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, &rule, 0) != 0) {
        close(fd);
        fail("could not add Landlock allowlist rule");
    }
    close(fd);
}

static void apply_landlock(bool update_profile, const char *update_target) {
    int abi = landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
    if (abi < 3) fail("Landlock ABI 3 or newer is required");

    uint64_t handled = write_access();
    struct landlock_ruleset_attr ruleset = {.handled_access_fs = handled};
    int ruleset_fd = landlock_create_ruleset(&ruleset, sizeof(ruleset), 0);
    if (ruleset_fd < 0) fail("could not create Landlock ruleset");

    allow_path(ruleset_fd, "/lib", read_access());
    allow_path(ruleset_fd, "/lib64", read_access());
    allow_path(ruleset_fd, "/usr/lib", read_access());
    allow_path(ruleset_fd, "/usr/lib64", read_access());
    allow_path(ruleset_fd, "/etc/ld.so.cache", read_access());
    allow_path(ruleset_fd, "/dev/null", write_access());
    allow_path(ruleset_fd, "/dev/urandom", read_access());
    if (update_profile) {
        allow_path(ruleset_fd, FRESHCLAM_BINARY, read_access());
        allow_path(ruleset_fd, FRESHCLAM_CONFIG, read_access());
        allow_path(ruleset_fd, "/etc/passwd", read_access());
        allow_path(ruleset_fd, "/etc/group", read_access());
        allow_path(ruleset_fd, "/etc/resolv.conf", read_access());
        allow_path(ruleset_fd, "/etc/hosts", read_access());
        allow_path(ruleset_fd, "/etc/host.conf", read_access());
        allow_path(ruleset_fd, "/etc/gai.conf", read_access());
        allow_path(ruleset_fd, "/etc/nsswitch.conf", read_access());
        allow_path(ruleset_fd, "/etc/services", read_access());
        allow_path(ruleset_fd, "/etc/ssl/certs", read_access());
        allow_path(ruleset_fd, "/etc/ssl/openssl.cnf", read_access());
        allow_path(ruleset_fd, "/etc/localtime", read_access());
        allow_path(ruleset_fd, "/etc/clamav", read_access());
        allow_path(ruleset_fd, "/usr/share/clamav", read_access());
        allow_path(ruleset_fd, "/usr/share/ca-certificates", read_access());
        allow_path(ruleset_fd, "/usr/share/zoneinfo", read_access());
        allow_path(ruleset_fd, update_target, write_access());
    } else {
        allow_path(ruleset_fd, CLAMSCAN_BINARY, read_access());
        allow_path(ruleset_fd, DEFINITION_SETS_ROOT, read_access());
        allow_path(ruleset_fd, SCAN_TEMP_ROOT, write_access());
    }
    if (landlock_restrict_self(ruleset_fd, 0) != 0) {
        close(ruleset_fd);
        fail("could not activate Landlock ruleset");
    }
    close(ruleset_fd);
}

static void close_extra_descriptors(void) {
    long maximum = sysconf(_SC_OPEN_MAX);
    if (maximum < 0 || maximum > 65536) maximum = 65536;
    for (int fd = 3; fd < maximum; fd++) close(fd);
}

static void minimal_environment(bool update_profile, const char *update_target) {
    if (clearenv() != 0) fail("could not clear environment");
    if (setenv("PATH", "/usr/bin:/bin", 1) != 0 ||
        setenv("HOME", SCAN_TEMP_ROOT, 1) != 0 ||
        setenv("LANG", "C", 1) != 0 || setenv("LC_ALL", "C", 1) != 0)
        fail("could not install minimal environment");
    if (setenv("TMPDIR", update_profile ? update_target : SCAN_TEMP_ROOT, 1) != 0)
        fail("could not configure sandbox temporary directory");
}

static void apply_common_boundary(bool update_profile, const char *update_target) {
    if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0) fail("could not disable dumps");
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) fail("could not enable no_new_privs");
    apply_resource_limits(update_profile);
    apply_landlock(update_profile, update_target);
    apply_seccomp(update_profile);
    verify_io_uring_denied();
    close_extra_descriptors();
    minimal_environment(update_profile, update_target);
}

static void require_path_denied(const char *path) {
    errno = 0;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd >= 0) {
        close(fd);
        fail("sandbox negative filesystem probe unexpectedly succeeded");
    }
    if (errno != EACCES && errno != EPERM)
        fail("sandbox negative filesystem probe could not prove denial");
}

static void verify_forbidden_paths(void) {
    /* These paths exist in the worker image. Denial proves the child has no
       Drive, application-secret, or proc-environment read path. */
    require_path_denied("/data");
    require_path_denied("/app/alembic.ini");
    require_path_denied("/proc/self/environ");
    char parent_environment[128];
    if (snprintf(parent_environment, sizeof(parent_environment),
                 "/proc/%ld/environ", (long)getppid()) >= (int)sizeof(parent_environment))
        fail("sandbox proc probe path overflow");
    require_path_denied(parent_environment);
}

static void verify_environment_scrubbed(void) {
    for (char **entry = environ; entry != NULL && *entry != NULL; entry++) {
        if (strncmp(*entry, "ONEBRAIN_", 9) == 0)
            fail("sandbox environment retained an application setting");
    }
}

static void verify_scan_boundary(void) {
    verify_forbidden_paths();

    errno = 0;
    int network = socket(AF_INET, SOCK_STREAM, 0);
    if (network >= 0) {
        close(network);
        fail("sandbox negative network probe unexpectedly succeeded");
    }
    if (errno != EACCES && errno != EPERM)
        fail("sandbox negative network probe could not prove denial");
    verify_environment_scrubbed();
}

static void verify_update_boundary(void) {
    verify_forbidden_paths();
    int network = socket(AF_INET, SOCK_STREAM, 0);
    if (network < 0) fail("definition update sandbox unexpectedly denied socket creation");
    close(network);
    verify_environment_scrubbed();
}

static int scan_profile(int argc, char **argv) {
    if (argc < 3 || argc > MAX_CHILD_ARGS) fail("invalid scan argument count");
    int database = 0, stdout_flag = 0, summary = 0, archive = 0, exceeds = 0;
    int encrypted = 0, official = 0, scan_size = 0, file_size = 0, files = 0;
    int recursion = 0, scan_time = 0, bytecode_time = 0;
    int stdin_target = 0, version = 0;
    for (int i = 2; i < argc; i++) {
        if (!valid_scan_argument(argv[i])) fail("scan argument is not allowlisted");
        if (strncmp(argv[i], "--database=", 11) == 0) database++;
        else if (strcmp(argv[i], "--stdout") == 0) stdout_flag++;
        else if (strcmp(argv[i], "--no-summary") == 0) summary++;
        else if (strcmp(argv[i], "--scan-archive=yes") == 0) archive++;
        else if (strcmp(argv[i], "--alert-exceeds-max=yes") == 0) exceeds++;
        else if (strcmp(argv[i], "--alert-encrypted=yes") == 0) encrypted++;
        else if (strcmp(argv[i], "--official-db-only=yes") == 0) official++;
        else if (strncmp(argv[i], "--max-scansize=", 15) == 0) scan_size++;
        else if (strncmp(argv[i], "--max-filesize=", 15) == 0) file_size++;
        else if (strncmp(argv[i], "--max-files=", 12) == 0) files++;
        else if (strncmp(argv[i], "--max-recursion=", 16) == 0) recursion++;
        else if (strncmp(argv[i], "--max-scantime=", 15) == 0) scan_time++;
        else if (strncmp(argv[i], "--bytecode-timeout=", 19) == 0) bytecode_time++;
        else if (strcmp(argv[i], "-") == 0) stdin_target++;
        else if (strcmp(argv[i], "--version") == 0) version++;
    }
    if (version == 1 && argc == 5 && database == 1 && official == 1) {
        /* The readiness probe needs only an immutable database and --version. */
    } else if (!(version == 0 && database == 1 && stdout_flag == 1 && summary == 1 &&
                 archive == 1 && exceeds == 1 && encrypted == 1 && official == 1 &&
                 scan_size == 1 && file_size == 1 && files == 1 && recursion == 1 &&
                 scan_time == 1 && bytecode_time == 1 && stdin_target == 1 &&
                 argc == 16 && strcmp(argv[argc - 1], "-") == 0)) {
        fail("scan profile requires the complete fail-closed argument set");
    }
    apply_common_boundary(false, SCAN_TEMP_ROOT);
    verify_scan_boundary();
    if (chdir(SCAN_TEMP_ROOT) != 0) fail("scanner temporary directory is unavailable");

    char *child[MAX_CHILD_ARGS];
    child[0] = (char *)CLAMSCAN_BINARY;
    for (int i = 2; i < argc; i++) child[i - 1] = argv[i];
    child[argc - 1] = NULL;
    execv(CLAMSCAN_BINARY, child);
    fail("could not execute clamscan");
    return EXIT_SANDBOX_FAILURE;
}

static int update_profile(int argc, char **argv) {
    if (argc != 3) fail("definitions-update requires one target directory");
    char resolved[4096];
    if (realpath(argv[2], resolved) == NULL || !starts_with_path(resolved, DEFINITION_INCOMING_ROOT))
        fail("definition update target is outside the private incoming directory");
    apply_common_boundary(true, resolved);
    verify_update_boundary();
    if (chdir(resolved) != 0) fail("definition update target is unavailable");
    char datadir[4352];
    if (snprintf(datadir, sizeof(datadir), "--datadir=%s", resolved) >= (int)sizeof(datadir))
        fail("definition update target is too long");
    char *child[] = {
        (char *)FRESHCLAM_BINARY,
        "--config-file=" FRESHCLAM_CONFIG,
        datadir,
        "--stdout",
        "--no-warnings",
        NULL,
    };
    execv(FRESHCLAM_BINARY, child);
    fail("could not execute freshclam");
    return EXIT_SANDBOX_FAILURE;
}

int main(int argc, char **argv) {
    if (argc < 2) fail("a profile is required");
    if (geteuid() == 0) fail("refusing to scan as root");
    if (strcmp(argv[1], "scan") == 0) return scan_profile(argc, argv);
    if (strcmp(argv[1], "definitions-update") == 0) return update_profile(argc, argv);
    fail("unknown profile");
    return EXIT_SANDBOX_FAILURE;
}
