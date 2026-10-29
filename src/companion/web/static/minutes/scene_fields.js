// 場面と授業を選ぶ欄（_scene_fields.html）.
// 授業を選んだときだけ授業の欄を出し、題名を手で変えていなければ、場面や授業に合わせて題名も変える。
(() => {
  for (const kindSelect of document.querySelectorAll("[data-scene-kind]")) {
    const form = kindSelect.form;
    const courseRow = form.querySelector("[data-scene-course]");
    const courseSelect = courseRow ? courseRow.querySelector("select") : null;
    const title = form.querySelector('input[name="title"]');

    const suggested = () => {
      const course = courseSelect && !courseRow.hidden ? courseSelect.selectedOptions[0] : null;
      if (course && course.value) return course.dataset.defaultTitle;
      return kindSelect.selectedOptions[0].dataset.defaultTitle;
    };
    let lastSuggested = suggested();

    const update = () => {
      if (courseRow) courseRow.hidden = kindSelect.value !== "lecture";
      const next = suggested();
      // 題名が前の案のまま（手で変えていない）なら、新しい案に入れ替える.
      if (title && (title.value === lastSuggested || title.value === "")) title.value = next;
      lastSuggested = next;
    };
    kindSelect.addEventListener("change", update);
    if (courseSelect) courseSelect.addEventListener("change", update);
  }
})();
