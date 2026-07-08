"use strict";
const electron = require("electron");
const path = require("path");
const utils = require("@electron-toolkit/utils");
const child_process = require("child_process");
const path$1 = require("node:path");
const fs = require("node:fs");
const fs$1 = require("fs");
const net = require("node:net");
const os = require("node:os");
function _interopNamespaceDefault(e) {
  const n = Object.create(null, { [Symbol.toStringTag]: { value: "Module" } });
  if (e) {
    for (const k in e) {
      if (k !== "default") {
        const d = Object.getOwnPropertyDescriptor(e, k);
        Object.defineProperty(n, k, d.get ? d : {
          enumerable: true,
          get: () => e[k]
        });
      }
    }
  }
  n.default = e;
  return Object.freeze(n);
}
const fs__namespace = /* @__PURE__ */ _interopNamespaceDefault(fs);
const net__namespace = /* @__PURE__ */ _interopNamespaceDefault(net);
const icon = path.join(__dirname, "../../resources/icon.png");
let pythonProcess = null;
let gradioPort = 0;
let rootPath = "";
function getCpuSnapshot() {
  const cpus = os.cpus();
  let idle = 0;
  let total = 0;
  for (const cpu of cpus) {
    idle += cpu.times.idle;
    total += Object.values(cpu.times).reduce((sum, value) => sum + value, 0);
  }
  return { idle, total };
}
let lastCpuSnapshot = getCpuSnapshot();
function readCpuUsage() {
  const current = getCpuSnapshot();
  const idleDelta = current.idle - lastCpuSnapshot.idle;
  const totalDelta = current.total - lastCpuSnapshot.total;
  lastCpuSnapshot = current;
  if (totalDelta <= 0) return 0;
  return Math.max(0, Math.min(100, (1 - idleDelta / totalDelta) * 100));
}
function readGpuStats() {
  return new Promise((resolve) => {
    child_process.execFile(
      "nvidia-smi",
      ["--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
      { windowsHide: true, timeout: 2500 },
      (error, stdout) => {
        if (error || !stdout) {
          resolve(null);
          return;
        }
        const line = stdout.trim().split(/\r?\n/)[0] || "";
        const parts = line.split(",").map((part) => Number(part.trim()));
        if (parts.length < 3 || parts.some((value) => Number.isNaN(value))) {
          resolve(null);
          return;
        }
        resolve({ percent: parts[0], memoryUsed: parts[1] * 1024 * 1024, memoryTotal: parts[2] * 1024 * 1024 });
      }
    );
  });
}
if (electron.app.isPackaged) {
  rootPath = path$1.dirname(electron.app.getPath("exe"));
} else {
  rootPath = path$1.join(__dirname, "../../");
}
const configPath = path$1.join(rootPath, "yzy_config.json");
const findGradioPort = (resolve, text) => {
  try {
    const msg = JSON.parse(text);
    if (msg.server_port) {
      console.log(`gradio server port: ${msg.server_port}`);
      gradioPort = msg.server_port;
      resolve(gradioPort);
      return;
    }
  } catch (_) {
  }
};
function startGradio(win, genType) {
  console.log(genType);
  return new Promise((resolve) => {
    let pyEnv = "";
    if (electron.app.isPackaged) {
      pyEnv = path.join(path$1.dirname(electron.app.getPath("exe")), "python/build_venv/python/python.exe");
    } else {
      pyEnv = path.join(__dirname, "../../python/build_venv/python/python.exe");
    }
    if (!fs$1.existsSync(pyEnv)) {
      win.webContents.send(
        `gradio-log`,
        `【严重错误】未检测到 Python 环境，请确认 python 文件夹是否已拷贝到 exe 同级目录!`
      );
      return;
    }
    const demo_path = path.join(rootPath, "python", "entry_yzy.py");
    pythonProcess = child_process.spawn(pyEnv, ["-u", demo_path], {
      cwd: path.join(rootPath, "python"),
      env: {
        ...process.env,
        // 继承系统原有的环境变量
        PYTHONIOENCODING: "utf-8",
        // 强制 Python 输入输出使用 utf-8
        PYTHONLEGACYWINDOWSSTDIO: "utf-8"
        // 针对部分新版 Python/Windows 的补丁
      }
    });
    pythonProcess.stdout.on("data", (data) => {
      const text = data.toString();
      findGradioPort(resolve, text);
      win.webContents.send("gradio-log", (/* @__PURE__ */ new Date()).toLocaleString() + " " + text);
    });
    pythonProcess.stderr.on("data", (data) => {
      win.webContents.send("gradio-log", (/* @__PURE__ */ new Date()).toLocaleString() + " [ERR] " + data.toString());
    });
    pythonProcess.on("close", () => {
      win.webContents.send("gradio-log", (/* @__PURE__ */ new Date()).toLocaleString() + " [Python] 已退出");
    });
    setTimeout(() => {
      win.webContents.send(`gradio-log`, pyEnv);
      win.webContents.send(`gradio-log`, demo_path);
    }, 1e3);
  });
}
const exitGradio = () => {
  if (pythonProcess) {
    try {
      child_process.spawn("taskkill", ["/pid", pythonProcess.pid.toString(), "/T", "/F"]);
      pythonProcess = null;
      console.log(`Gradio process killed (server port ${gradioPort})`);
    } catch (e) {
      console.log(e);
    }
  }
};
function createWindow() {
  const mainWindow = new electron.BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 768,
    // 最小宽度
    minHeight: 480,
    // 最小高度
    show: false,
    autoHideMenuBar: true,
    icon: path.join(__dirname, "../../resources/icon.png"),
    ...process.platform === "linux" ? { icon } : {},
    webPreferences: {
      preload: path.join(__dirname, "../preload/index.js"),
      sandbox: false,
      // 关键配置：禁止后台休眠/节流
      // 这会让定时器在最小化时依然按原速度运行
      backgroundThrottling: false
    }
  });
  startGradio(mainWindow, "");
  mainWindow.on("ready-to-show", () => {
    mainWindow.show();
  });
  mainWindow.webContents.setWindowOpenHandler((details) => {
    electron.shell.openExternal(details.url);
    return { action: "deny" };
  });
  if (utils.is.dev && process.env["ELECTRON_RENDERER_URL"]) {
    mainWindow.loadURL(process.env["ELECTRON_RENDERER_URL"]);
  } else {
    mainWindow.loadFile(path.join(__dirname, "../renderer/index.html"));
  }
}
electron.app.whenReady().then(() => {
  utils.electronApp.setAppUserModelId("com.electron");
  electron.app.on("browser-window-created", (_, window) => {
    utils.optimizer.watchWindowShortcuts(window);
  });
  createWindow();
  electron.app.on("activate", function() {
    if (electron.BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});
electron.app.on("window-all-closed", () => {
  exitGradio();
  if (process.platform !== "darwin") {
    electron.app.quit();
  }
});
electron.app.on("will-quit", () => {
  exitGradio();
});
electron.ipcMain.handle("open-folder", async (_event, folderPath) => {
  const pth = path.join(rootPath, folderPath);
  console.log(`open folder ${pth}`);
  if (!pth) return;
  await electron.shell.openPath(pth);
});
electron.ipcMain.on("save-config", (event, newConfig) => {
  try {
    fs__namespace.writeFileSync(configPath, JSON.stringify(newConfig, null, 2));
    console.log("Config saved:", newConfig);
    event.reply("config-saving");
  } catch (err) {
    console.error("Failed to save config:", err);
  }
});
electron.ipcMain.on("start-gradio", (event, config) => {
  try {
    console.log(`start-gradio config: ${config}`);
    const mainWindow = require("electron").BrowserWindow.getAllWindows()[0];
    startGradio(mainWindow, config.genType);
    event.reply("gradio-tarting");
  } catch (err) {
    console.error("Failed to start gradio:", err);
  }
});
electron.ipcMain.on("stop-gradio", (event, config) => {
  try {
    console.log(`stop-gradio config: ${config}`);
    exitGradio();
    event.reply("gradio-stopping");
  } catch (err) {
    console.error("Failed to stop gradio:", err);
  }
});
function loadConfig() {
  try {
    if (fs__namespace.existsSync(configPath)) {
      const data = fs__namespace.readFileSync(configPath, "utf-8");
      return JSON.parse(data);
    }
  } catch (err) {
    console.error("read config error:", err);
  }
  return {};
}
electron.ipcMain.handle("get-config", async (_event) => {
  const config = loadConfig();
  console.log("React request config，return:", config);
  return config;
});
electron.ipcMain.handle("get-system-load", async (_event) => {
  const memoryTotal = os.totalmem();
  const memoryUsed = memoryTotal - os.freemem();
  const gpu = await readGpuStats();
  return {
    cpu: {
      percent: readCpuUsage(),
      cores: os.cpus().length
    },
    memory: {
      used: memoryUsed,
      total: memoryTotal
    },
    gpu
  };
});
async function hasProcessByCommandLine(keyword) {
  return new Promise((resolve, reject) => {
    const script = `
$found = Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like "*$env:PROCESS_KEYWORD*" } |
  Select-Object -First 1
if ($found) { exit 0 } else { exit 1 }
`;
    const child = child_process.spawn(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
      {
        windowsHide: true,
        env: {
          ...process.env,
          PROCESS_KEYWORD: keyword
        }
      }
    );
    let error = "";
    child.stderr.on("data", (data) => {
      error += data.toString();
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) return resolve(true);
      if (code === 1) return resolve(false);
      reject(new Error(error || `PowerShell exited with code ${code}`));
    });
  });
}
electron.ipcMain.handle("check-port", async (_event) => {
  return new Promise((resolve) => {
    console.log(gradioPort);
    const socket = new net__namespace.Socket();
    socket.setTimeout(5e3);
    const cleanUp = () => {
      socket.destroy();
      socket.unref();
    };
    socket.on("connect", () => {
      cleanUp();
      resolve(`http://127.0.0.1:${gradioPort}/`);
    });
    socket.on("error", async () => {
      cleanUp();
      let process2 = await hasProcessByCommandLine("entry_yzy");
      let msg = process2 ? "" : "error";
      resolve(msg);
    });
    socket.on("timeout", () => {
      cleanUp();
      resolve("");
    });
    socket.connect(gradioPort, "127.0.0.1");
  });
});
