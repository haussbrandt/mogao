document.addEventListener("DOMContentLoaded", async () => {
  const videoBasePath = document.body.dataset.videoBasePath;
  const grid = document.querySelector(".video-grid");
  const selector = document.getElementById("sort-selector");
  const cards = Array.from(grid.querySelectorAll(".video-card"));
  const uploadInput = document.getElementById("video-upload");
  const uploadButton = document.querySelector(".upload-btn");
  const uploadStatus = document.getElementById("upload-status");
  const uploadStatusText = document.getElementById("upload-status-text");
  const uploadProgress = document.getElementById("upload-progress");

  const responseJson = async (response) => {
    if (response.ok) return response.json();
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body.detail) message = body.detail;
    } catch (_) {}
    throw new Error(message);
  };

  const uploadChunk = async (uploadId, chunkIndex, chunk) => {
    let lastError;
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const response = await fetch(
          `${videoBasePath}/upload-chunks/${uploadId}/${chunkIndex}`,
          { method: "PUT", body: chunk },
        );
        await responseJson(response);
        return;
      } catch (error) {
        lastError = error;
        if (attempt < 3) {
          await new Promise((resolve) => setTimeout(resolve, attempt * 1000));
        }
      }
    }
    throw lastError;
  };

  const waitForProcessing = async (processingId) => {
    while (true) {
      const response = await fetch(
        `${videoBasePath}/upload-chunks/${processingId}/status`,
      );
      const processing = await responseJson(response);
      if (processing.status === "complete") return processing;
      if (processing.status === "failed") {
        throw new Error(processing.detail || "Video processing failed");
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  };

  const uploadFile = async (file, fileIndex, fileCount) => {
    const startResponse = await fetch(`${videoBasePath}/upload-chunks/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, size: file.size }),
    });
    const upload = await responseJson(startResponse);

    try {
      uploadStatusText.textContent =
        `Uploading ${fileIndex + 1}/${fileCount}: ${file.name} (0%)`;
      for (let chunkIndex = 0; chunkIndex < upload.total_chunks; chunkIndex++) {
        const start = chunkIndex * upload.chunk_size;
        const chunk = file.slice(
          start,
          Math.min(start + upload.chunk_size, file.size),
        );
        await uploadChunk(upload.upload_id, chunkIndex, chunk);

        const percent = Math.round(
          ((chunkIndex + 1) / upload.total_chunks) * 100,
        );
        uploadProgress.value = percent;
        uploadStatusText.textContent =
          `Uploading ${fileIndex + 1}/${fileCount}: ${file.name} (${percent}%)`;
      }

      uploadStatusText.textContent = `Preparing ${file.name} for processing...`;
      const completeResponse = await fetch(
        `${videoBasePath}/upload-chunks/${upload.upload_id}/complete`,
        { method: "POST" },
      );
      return await responseJson(completeResponse);
    } catch (error) {
      await fetch(`${videoBasePath}/upload-chunks/${upload.upload_id}`, {
        method: "DELETE",
      }).catch(() => {});
      throw error;
    }
  };

  uploadInput.addEventListener("change", async () => {
    const files = Array.from(uploadInput.files);
    if (!files.length) return;

    uploadInput.disabled = true;
    uploadButton.classList.add("disabled");
    uploadStatus.hidden = false;
    uploadStatus.classList.remove("error");
    uploadProgress.value = 0;

    try {
      const processingJobs = [];
      for (let fileIndex = 0; fileIndex < files.length; fileIndex++) {
        const job = await uploadFile(files[fileIndex], fileIndex, files.length);
        processingJobs.push(job);
      }
      uploadProgress.value = 100;
      uploadStatusText.textContent =
        `Upload complete. Processing 0/${processingJobs.length} videos...`;

      let processedCount = 0;
      await Promise.all(
        processingJobs.map(async (job) => {
          await waitForProcessing(job.processing_id);
          processedCount++;
          uploadStatusText.textContent =
            `Processing complete for ${processedCount}/${processingJobs.length} videos...`;
        }),
      );
      uploadStatusText.textContent = "Processing complete. Refreshing library...";
      window.location.reload();
    } catch (error) {
      uploadStatus.classList.add("error");
      uploadStatusText.textContent =
        `Upload or processing failed: ${error.message}`;
    } finally {
      uploadInput.disabled = false;
      uploadButton.classList.remove("disabled");
      uploadInput.value = "";
    }
  });

  document.getElementById("yt-submit").addEventListener("click", async () => {
    const url = document.getElementById("yt-link").value.trim();
    if (!url) return;
    await fetch(`${videoBasePath}/download-video`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
  });

  cards.forEach((card) => {
    card.addEventListener("click", () => {
      window.location.href = card.dataset.watchUrl;
    });
    const deleteForm = card.querySelector(".delete-form");
    deleteForm.addEventListener("click", (event) => event.stopPropagation());
    deleteForm.addEventListener("submit", (event) => {
      if (!confirm("Are you sure you want to delete this video?")) {
        event.preventDefault();
      }
    });
  });

  try {
    const response = await fetch(`${videoBasePath}/api/get-settings`);
    const settings = await response.json();
    if (settings.sort_order) selector.value = settings.sort_order;
  } catch (error) {
    console.error("Could not load settings", error);
  }

  const sortVideos = () => {
    const criteria = selector.value;
    cards.sort((a, b) => {
      switch (criteria) {
        case "title":
          return a.dataset.title.localeCompare(b.dataset.title);
        case "title_rev":
          return b.dataset.title.localeCompare(a.dataset.title);
        case "last_watch":
          return Number(b.dataset.watch) - Number(a.dataset.watch);
        case "last_watch_rev":
          return Number(a.dataset.watch) - Number(b.dataset.watch);
        case "date_added":
          return b.dataset.added.localeCompare(a.dataset.added);
        case "date_added_rev":
          return a.dataset.added.localeCompare(b.dataset.added);
        case "chars":
          return Number(b.dataset.chars) - Number(a.dataset.chars);
        case "chars_rev":
          return Number(a.dataset.chars) - Number(b.dataset.chars);
        case "length":
          return Number(b.dataset.length) - Number(a.dataset.length);
        case "length_rev":
          return Number(a.dataset.length) - Number(b.dataset.length);
        case "mined":
          return Number(b.dataset.mined) - Number(a.dataset.mined);
        case "mined_rev":
          return Number(a.dataset.mined) - Number(b.dataset.mined);
        default:
          return 0;
      }
    });
    cards.forEach((card) => grid.appendChild(card));
  };

  selector.addEventListener("change", async () => {
    sortVideos();
    await fetch(`${videoBasePath}/api/save-sort`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sort_order: selector.value }),
    });
  });

  sortVideos();
});
