// Fetches data.json from the same module and renders it.
fetch("/static/data.json")
    .then((response) => response.json())
    .then((data) => {
        document.getElementById("data").textContent = JSON.stringify(data, null, 2);
    })
    .catch((error) => {
        document.getElementById("data").textContent = `failed to load data.json: ${error}`;
    });
