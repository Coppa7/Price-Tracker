document.addEventListener("DOMContentLoaded", function() {
    const charts = document.querySelectorAll(".bookmark_chart");

    charts.forEach(canvas => {
        const asin = canvas.getAttribute("data-asin");
        const ctx = canvas.getContext("2d");

        fetch(`/bookmark_info/${asin}`) 
        .then(response => response.json())
        .then(data => {
            new Chart(ctx, {
                type: "line",
                data: {
                    labels: data.dates, 
                    datasets: [{
                        label: 'Prezzo (€)',
                        data: data.prices, 
                        borderColor: '#ff9900',
                        backgroundColor: 'rgba(255, 153, 0, 0.1)',
                        borderWidth: 2,
                        tension: 0.3
                    }]
                },
                options: {
                    responsive: true,
                    scales: {
                        y: { beginAtZero: false }
                    }
                }
            });
        });
    });
});