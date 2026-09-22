from fastapi import Request
from fastapi.responses import JSONResponse

def add_exception_handlers(app):
    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        # In a real project you would log the exception details here
        return JSONResponse(status_code=500, content={"detail": f"Internal server error: {str(exc)}"})
