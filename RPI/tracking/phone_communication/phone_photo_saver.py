from full_communication import *
SAVE_DIR="images/"
SAVE_PATH=os.path.join(SAVE_DIR, f"last.jpg")


def save_photo(frame):
    cv2.imwrite(SAVE_PATH, frame)

if __name__=="__main__":
    print(f"Saving to {SAVE_PATH}")
    register_camera_callback(save_photo)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Interrupted by user — exiting")
