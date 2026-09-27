// code related to showing and updating progressbar shown as the image is being made

function rememberGallerySelection() { }

function getGallerySelectedIndex() { }

function request(url, data, handler, errorHandler) {
    let xhr = new XMLHttpRequest();
    xhr.open("POST", url, true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onreadystatechange = function () {
        if (xhr.readyState === 4) {
            if (xhr.status === 200) {
                try {
                    let js = JSON.parse(xhr.responseText);
                    handler(js);
                } catch (error) {
                    console.error(error);
                    errorHandler();
                }
            } else {
                errorHandler();
            }
        }
    };
    let js = JSON.stringify(data);
    xhr.send(js);
}

function pad2(x) {
    return x < 10 ? "0" + x : x;
}

function formatTime(secs) {
    if (secs > 3600) {
        return (
            pad2(Math.floor(secs / 60 / 60)) + ":" + pad2(Math.floor(secs / 60) % 60) + ":" + pad2(Math.floor(secs) % 60)
        );
    } else if (secs > 60) {
        return pad2(Math.floor(secs / 60)) + ":" + pad2(Math.floor(secs) % 60);
    } else {
        return Math.floor(secs) + "s";
    }
}

let originalAppTitle = undefined;

onUiLoaded(function () {
    originalAppTitle = document.title;
});

function setTitle(progress) {
    let title = originalAppTitle;

    if (opts.show_progress_in_title && progress) {
        title = "[" + progress.trim() + "] " + title;
    }

    if (document.title != title) {
        document.title = title;
    }
}

function randomId() {
    return (
        "task(" +
        Math.random().toString(36).slice(2, 7) +
        Math.random().toString(36).slice(2, 7) +
        Math.random().toString(36).slice(2, 7) +
        ")"
    );
}

// the longest dimension, in device pixels, that a live preview image needs to have
// to fill the gallery it is shown in; used to downscale previews server-side so
// mobile browsers don't get killed by full-size data URIs
function livePreviewMaxSize(gallery) {
    let size = 0;
    if (gallery) {
        size = Math.max(gallery.clientWidth, gallery.clientHeight);
    }
    if (!size || opts.js_live_preview_in_modal_lightbox) {
        size = Math.max(size, window.innerWidth, window.innerHeight);
    }
    let dpr = window.devicePixelRatio || 1;
    return Math.min(Math.ceil(size * dpr), 4096);
}

// starts sending progress requests to "/internal/progress" uri, creating progressbar above progressbarContainer element and
// preview inside gallery element. Cleans up all created stuff when the task is over and calls atEnd.
// calls onProgress every time there is a progress update
function requestProgress(id_task, progressbarContainer, gallery, atEnd, onProgress, inactivityTimeout = 40) {
    let dateStart = new Date();
    let wasEverActive = false;
    let parentProgressbar = progressbarContainer.parentNode;
    let wakeLock = null;

    // tab of the localStorage task id matching this task, if any; only such
    // tasks can be revived through the restore-progress path after the browser
    // suspends the page
    let restoreTab = typeof taskRestoreTab === "function" ? taskRestoreTab(id_task) : null;
    let canAutoRestore = function () {
        return restoreTab && opts.js_restore_progress_on_return;
    };

    if (gallery && gallery.classList.contains("hidden")) gallery = gallery.parentElement.querySelector(".gradio-video");

    let requestWakeLock = async function () {
        if (!opts.prevent_screen_sleep_during_generation || wakeLock || document.hidden) return;
        try {
            wakeLock = await navigator.wakeLock.request("screen");
            // the browser releases the lock itself when the page is hidden;
            // forget it so the next progress tick can re-acquire
            wakeLock.addEventListener("release", function () {
                wakeLock = null;
            });
        } catch (err) {
            console.error("Wake Lock is not supported.");
        }
    };

    let releaseWakeLock = async function () {
        if (!opts.prevent_screen_sleep_during_generation || !wakeLock) return;
        try {
            await wakeLock.release();
            wakeLock = null;
        } catch (err) {
            console.error("Wake Lock release failed", err);
        }
    };

    let divProgress = document.createElement("div");
    divProgress.className = "progressDiv";
    divProgress.style.display = opts.show_progressbar ? "block" : "none";
    let divInner = document.createElement("div");
    divInner.className = "progress";

    divProgress.appendChild(divInner);
    parentProgressbar.insertBefore(divProgress, progressbarContainer);

    let livePreview = null;
    let suspended = false;
    let hiddenAt = null;
    let consecutiveErrors = 0;
    let livePreviewErrors = 0;

    let clearProgressUI = function () {
        releaseWakeLock();
        if (!divProgress) return false;

        setTitle("");
        parentProgressbar.removeChild(divProgress);
        if (gallery && livePreview) gallery.removeChild(livePreview);

        divProgress = null;
        livePreview = null;
        return true;
    };

    let removeProgressBar = function () {
        document.removeEventListener("visibilitychange", onVisibilityChange);
        if (clearProgressUI()) atEnd();
    };

    // stop polling but keep the task restorable (no atEnd, task id stays in
    // localStorage); used when the browser suspends the page and the
    // connections can no longer be trusted
    let suspendProgress = function () {
        suspended = true;
        clearProgressUI();
    };

    let onVisibilityChange = function () {
        if (document.hidden) {
            // Date.now, not performance.now: the monotonic clock pauses during
            // phone deep sleep, which would make a long screen-off look short
            if (hiddenAt === null) hiddenAt = Date.now();
            return;
        }

        let hiddenFor = hiddenAt === null ? 0 : Date.now() - hiddenAt;
        hiddenAt = null;

        if (!canAutoRestore()) return;
        if (!suspended && hiddenFor < 8000) return;

        // the OS likely froze the page while it was hidden: the connection
        // delivering the generation result may be dead even if our polling can
        // resume, so reattach through the restore-progress path instead
        document.removeEventListener("visibilitychange", onVisibilityChange);
        suspendProgress();
        restoreProgressTask(restoreTab);
    };

    document.addEventListener("visibilitychange", onVisibilityChange);

    let funProgress = function (id_task) {
        if (!divProgress) return;
        requestWakeLock();
        request(
            "./internal/progress",
            { id_task: id_task, live_preview: false },
            function (res) {
                if (!divProgress) return;
                consecutiveErrors = 0;

                if (res.completed) {
                    removeProgressBar();
                    return;
                }

                let progressText = "";

                divInner.style.width = (res.progress || 0) * 100.0 + "%";
                divInner.style.background = res.progress ? "" : "transparent";

                if (res.progress > 0) {
                    progressText = ((res.progress || 0) * 100.0).toFixed(0) + "%";
                }

                if (res.eta) {
                    progressText += " ETA: " + formatTime(res.eta);
                }

                setTitle(progressText);

                if (res.textinfo && res.textinfo.indexOf("\n") == -1) {
                    progressText = res.textinfo + " " + progressText;
                }

                divInner.textContent = progressText;

                let elapsedFromStart = (new Date() - dateStart) / 1000;

                if (res.active) wasEverActive = true;

                if (!res.active && wasEverActive) {
                    removeProgressBar();
                    return;
                }

                if (elapsedFromStart > inactivityTimeout && !res.queued && !res.active) {
                    removeProgressBar();
                    return;
                }

                if (onProgress) {
                    onProgress(res);
                }

                setTimeout(() => {
                    funProgress(id_task, res.id_live_preview);
                }, opts.live_preview_refresh_period || 500);
            },
            function () {
                // requests fail when the browser suspends a hidden page (phone
                // screen off, app switched); suspend and let the visibility
                // handler revive the task instead of tearing everything down
                if (canAutoRestore() && document.hidden) {
                    suspendProgress();
                    return;
                }

                consecutiveErrors++;
                if (consecutiveErrors <= 2) {
                    setTimeout(() => {
                        funProgress(id_task);
                    }, 1000 * consecutiveErrors);
                    return;
                }

                removeProgressBar();
            },
        );
    };

    let funLivePreview = function (id_task, id_live_preview) {
        if (!divProgress) return;
        request(
            "./internal/progress",
            {
                id_task: id_task,
                id_live_preview: id_live_preview,
                preview_max_size: livePreviewMaxSize(gallery),
            },
            function (res) {
                if (!divProgress) {
                    return;
                }
                livePreviewErrors = 0;

                if (res.live_preview && gallery) {
                    let img = new Image();
                    img.onload = function () {
                        if (!divProgress) return;

                        if (!livePreview) {
                            livePreview = document.createElement("div");
                            livePreview.className = "livePreview";
                            gallery.insertBefore(livePreview, gallery.firstElementChild);
                        }

                        livePreview.appendChild(img);
                        if (livePreview.childElementCount > 2) {
                            livePreview.removeChild(livePreview.firstElementChild);
                        }
                    };
                    img.src = res.live_preview;
                }

                setTimeout(() => {
                    funLivePreview(id_task, res.id_live_preview);
                }, opts.live_preview_refresh_period || 500);
            },
            function () {
                // the live preview is best-effort: losing it should not kill
                // the progress bar; the main progress loop owns teardown
                livePreviewErrors++;
                if (livePreviewErrors <= 2) {
                    setTimeout(() => {
                        funLivePreview(id_task, id_live_preview);
                    }, 2000 * livePreviewErrors);
                }
            },
        );
    };

    funProgress(id_task, 0);

    if (gallery) {
        funLivePreview(id_task, 0);
    }
}
