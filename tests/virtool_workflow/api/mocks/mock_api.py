from . import mock_analysis_routes, mock_index_routes, mock_job_routes

mock_routes = [*mock_analysis_routes.mock_routes,
               *mock_job_routes.mock_routes,
               *mock_index_routes.mock_routes]