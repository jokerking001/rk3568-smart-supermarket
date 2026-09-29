#include <asm/termbits.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/select.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <vector>

static size_t read_for(int fd, double seconds, std::vector<unsigned char>& out) {
    const long loops = static_cast<long>(seconds * 20.0);
    for (long i = 0; i < loops; ++i) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(fd, &rfds);
        timeval tv{0, 50000};
        int ready = select(fd + 1, &rfds, nullptr, nullptr, &tv);
        if (ready > 0) {
            unsigned char buf[8192];
            int n = read(fd, buf, sizeof(buf));
            if (n > 0) out.insert(out.end(), buf, buf + n);
        }
    }
    return out.size();
}

int main(int argc, char** argv) {
    const char* port = argc > 1 ? argv[1] : "/dev/lidar";
    int fd = open(port, O_RDWR | O_NOCTTY | O_NDELAY);
    if (fd < 0) {
        std::fprintf(stderr, "open: %s\n", std::strerror(errno));
        return 2;
    }
    termios2 tio{};
    if (ioctl(fd, TCGETS2, &tio) != 0) {
        std::fprintf(stderr, "TCGETS2: %s\n", std::strerror(errno));
        return 3;
    }
    tio.c_cflag &= ~CBAUD;
    tio.c_cflag |= BOTHER | CS8 | CREAD | CLOCAL;
    tio.c_cflag &= ~(PARENB | CSTOPB | CSIZE);
    tio.c_cflag |= CS8;
    tio.c_lflag &= ~(ICANON | ECHO | ECHOE | ISIG);
    tio.c_iflag &= ~(IXON | IXOFF | IXANY);
    tio.c_oflag &= ~OPOST;
    tio.c_ispeed = 150000;
    tio.c_ospeed = 150000;
    if (ioctl(fd, TCSETS2, &tio) != 0) {
        std::fprintf(stderr, "TCSETS2: %s\n", std::strerror(errno));
        return 4;
    }
    ioctl(fd, TCFLSH, TCIOFLUSH);

    std::vector<unsigned char> passive;
    read_for(fd, 3.0, passive);
    const unsigned char start[] = {0xA5, 0x60};
    ssize_t wrote = write(fd, start, sizeof(start));
    usleep(20000);
    std::vector<unsigned char> active;
    read_for(fd, 6.0, active);

    std::printf("passive=%zu active=%zu wrote=%zd\n", passive.size(), active.size(), wrote);
    size_t show = active.size() < 64 ? active.size() : 64;
    for (size_t i = 0; i < show; ++i) std::printf("%02x%s", active[i], i + 1 == show ? "\n" : " ");
    FILE* f = std::fopen("/home/linaro/ai/lidar/probe_termios2.bin", "wb");
    if (f) {
        std::fwrite(passive.data(), 1, passive.size(), f);
        std::fwrite(active.data(), 1, active.size(), f);
        std::fclose(f);
    }
    close(fd);
    return 0;
}
