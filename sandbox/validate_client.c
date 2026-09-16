#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define GATE_SOCKET "/run/streamstats-validation-gate.sock"

static int write_all(int fd, const char *buffer, size_t length) {
    while (length > 0) {
        ssize_t written = write(fd, buffer, length);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        buffer += written;
        length -= (size_t)written;
    }
    return 0;
}

int main(void) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        puts("validation could not proceed: validation service unavailable");
        return 1;
    }

    struct sockaddr_un address;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    strncpy(address.sun_path, GATE_SOCKET, sizeof(address.sun_path) - 1);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) < 0 ||
        write_all(fd, "VALIDATE\n", 9) < 0) {
        close(fd);
        puts("validation could not proceed: validation service unavailable");
        return 1;
    }

    char response[8192];
    size_t used = 0;
    while (used < sizeof(response) - 1) {
        ssize_t count = read(fd, response + used, sizeof(response) - 1 - used);
        if (count == 0) {
            break;
        }
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            close(fd);
            puts("validation could not proceed: validation service unavailable");
            return 1;
        }
        used += (size_t)count;
    }
    close(fd);
    response[used] = '\0';

    if (strncmp(response, "STATUS 0\n", 9) == 0) {
        fputs(response + 9, stdout);
        return 0;
    }
    if (strncmp(response, "STATUS 1\n", 9) == 0) {
        fputs(response + 9, stdout);
        return 1;
    }

    puts("validation could not proceed: validation service unavailable");
    return 1;
}
