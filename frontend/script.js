const form = document.getElementById("transcribe-form");
const submitButton = document.getElementById("submit-button");
const currentPanel = document.getElementById("current-panel");
const currentStep = document.getElementById("current-step");
const currentResult = document.getElementById("current-result");
const progressFill = document.getElementById("progress-fill");
const recentTasks = document.getElementById("recent-tasks");
let activeTask = null;
let pollTimer = null;

async function request(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "请求失败");
  return body;
}

function fileLink(id, kind, label) {
  const link = document.createElement("a");
  link.className = "file-link";
  link.href = "/api/task/" + id + "/file/" + kind;
  link.textContent = label;
  return link;
}

function renderTask(task) {
  currentPanel.hidden = false;
  currentStep.textContent = task.step || task.status;
  progressFill.style.width = String(task.progress || 0) + "%";
  currentResult.replaceChildren();
  if (task.status === "completed") {
    const title = document.createElement("p");
    title.textContent = task.result.title + " · " + task.result.segments + " 段";
    const files = document.createElement("div");
    files.className = "files";
    files.append(fileLink(task.task_id, "srt", "下载 SRT 字幕"));
    files.append(fileLink(task.task_id, "md", "下载 Markdown 转录"));
    currentResult.append(title, files);
  } else if (task.status === "failed") {
    const error = document.createElement("p");
    error.className = "error";
    error.textContent = task.error || "处理失败";
    currentResult.append(error);
  }
  submitButton.disabled = task.status === "queued" || task.status === "processing";
}

async function pollTask(id) {
  try {
    const task = await request("/api/task/" + id);
    renderTask(task);
    if (task.status === "completed" || task.status === "failed") {
      clearInterval(pollTimer);
      pollTimer = null;
      activeTask = null;
      loadTasks();
    }
  } catch (error) {
    currentStep.textContent = error.message;
    clearInterval(pollTimer);
    pollTimer = null;
    activeTask = null;
    submitButton.disabled = false;
  }
}

async function loadTasks() {
  try {
    const data = await request("/api/tasks");
    recentTasks.replaceChildren();
    if (!data.tasks.length) {
      recentTasks.textContent = "还没有任务";
      return;
    }
    for (const task of data.tasks) {
      const item = document.createElement("div");
      item.className = "task";
      const title = document.createElement("div");
      title.className = "task-title";
      title.textContent = (task.result && task.result.title) || task.url;
      const meta = document.createElement("div");
      meta.className = "task-meta";
      meta.textContent = (task.language === "en" ? "英语" : "中文") + " · " + (task.step || task.status);
      item.append(title, meta);
      if (task.status === "completed") {
        const links = document.createElement("div");
        const srt = document.createElement("a");
        srt.href = "/api/task/" + task.task_id + "/file/srt";
        srt.textContent = "SRT";
        const md = document.createElement("a");
        md.href = "/api/task/" + task.task_id + "/file/md";
        md.textContent = "Markdown";
        links.append(srt, md);
        item.append(links);
      }
      recentTasks.append(item);
    }
  } catch (error) {
    recentTasks.textContent = error.message;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (activeTask) return;
  submitButton.disabled = true;
  currentPanel.hidden = false;
  currentStep.textContent = "正在提交";
  currentResult.replaceChildren();
  progressFill.style.width = "0%";
  try {
    const data = await request("/api/transcribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        video_url: document.getElementById("video-url").value,
        language: form.elements.language.value,
      }),
    });
    activeTask = data.task_id;
    await pollTask(activeTask);
    if (activeTask) pollTimer = setInterval(() => pollTask(activeTask), 2000);
  } catch (error) {
    currentStep.textContent = error.message;
    submitButton.disabled = false;
    activeTask = null;
  }
});

loadTasks();
