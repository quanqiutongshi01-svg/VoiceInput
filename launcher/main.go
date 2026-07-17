// VoiceInput.exe — 便携版 GUI 启动器(windowsgui 子系统,无控制台)。
// 用内置 pythonw.exe 运行 app.py;pythonw 缺失时退回 python.exe。
// 启动失败弹原生 MessageBox 指向日志文件。
package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"unsafe"
)

var (
	user32      = syscall.NewLazyDLL("user32.dll")
	messageBoxW = user32.NewProc("MessageBoxW")
)

func msgBox(text, title string) {
	t, _ := syscall.UTF16PtrFromString(text)
	c, _ := syscall.UTF16PtrFromString(title)
	messageBoxW.Call(0, uintptr(unsafe.Pointer(t)), uintptr(unsafe.Pointer(c)), 0x10) // MB_ICONERROR
}

func main() {
	self, err := os.Executable()
	if err != nil {
		msgBox("无法定位程序目录", "语音输入")
		return
	}
	root := filepath.Dir(self)
	app := filepath.Join(root, "app")
	logPath := filepath.Join(app, "logs", "voiceinput.log")

	py := filepath.Join(root, "python", "pythonw.exe")
	if _, err := os.Stat(py); err != nil {
		py = filepath.Join(root, "python", "python.exe")
	}
	if _, err := os.Stat(py); err != nil {
		msgBox("找不到 python\\pythonw.exe\n请确认 VoiceInput 文件夹完整解压。", "语音输入")
		return
	}
	if _, err := os.Stat(filepath.Join(app, "app.py")); err != nil {
		msgBox("找不到 app\\app.py\n请确认 VoiceInput 文件夹完整解压。", "语音输入")
		return
	}

	cmd := exec.Command(py, "app.py")
	cmd.Dir = app
	if err := cmd.Start(); err != nil {
		msgBox(fmt.Sprintf("启动失败: %v\n日志: %s", err, logPath), "语音输入")
		return
	}
	// 不等待:启动器立即退出,pythonw 常驻托盘
}
