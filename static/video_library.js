document.addEventListener("DOMContentLoaded", async () => {
  const videoBasePath = document.body.dataset.videoBasePath;
  const cards = Array.from(document.querySelectorAll(".video-card"));
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
          { method: "PUT", headers: { "X-Mogao-Request": "1" }, body: chunk },
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
      headers: { "Content-Type": "application/json", "X-Mogao-Request": "1" },
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
        { method: "POST", headers: { "X-Mogao-Request": "1" } },
      );
      return await responseJson(completeResponse);
    } catch (error) {
      await fetch(`${videoBasePath}/upload-chunks/${upload.upload_id}`, {
        method: "DELETE",
        headers: { "X-Mogao-Request": "1" },
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

  const downloadInput = document.getElementById("yt-link");
  const downloadButton = document.getElementById("yt-submit");
  downloadButton.addEventListener("click", async () => {
    const url = downloadInput.value.trim();
    if (!url) return;

    downloadButton.disabled = true;
    downloadButton.textContent = "Checking...";

    try {
      const response = await fetch(`${videoBasePath}/download-video`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Mogao-Request": "1" },
        body: JSON.stringify({ url }),
      });
      await responseJson(response);
      downloadInput.value = "";
      downloadButton.textContent = "Started";
      downloadButton.classList.add("download-started");
    } catch (error) {
      console.error("Could not download video", error);
      downloadButton.textContent = "Failed";
      downloadButton.classList.add("download-failed");
    } finally {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      downloadButton.textContent = "Download";
      downloadButton.classList.remove("download-started", "download-failed");
      downloadButton.disabled = false;
    }
  });

  cards.forEach((card) => {
    card.addEventListener("click", () => {
      window.location.href = card.dataset.watchUrl;
    });
    const deleteForm = card.querySelector(".delete-form");
    deleteForm.addEventListener("click", (event) => event.stopPropagation());
  });
});
