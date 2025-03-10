import { useState, useEffect, useRef } from 'react';

export const RetryImageLoader = ({
    src,
    alt,
    maxRetries = 5,
    retryDelay = 3000,
}: {
    src: string;
    alt: string;
    maxRetries: number;
    retryDelay: number;
}) => {
    const [imageSrc, setImageSrc] = useState<string>(src);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let attempts = 0;
        let isCancelled = false;

        const loadImage = () => {
            const img = new Image();
            img.src = src;

            img.onload = () => {
                if (!isCancelled) {
                    setImageSrc(src);
                    setLoading(false);
                    setError(null);
                }
            };

            img.onerror = () => {
                if (attempts < maxRetries && !isCancelled) {
                    attempts++;
                    console.warn(`Retrying image load... Attempt ${attempts}`);
                    setTimeout(loadImage, retryDelay);
                } else if (!isCancelled) {
                    setError('Failed to load image after multiple attempts');
                    setLoading(false);
                }
            };
        };

        loadImage();

        return () => {
            isCancelled = true; // Cleanup in case the component is unmounted
        };
    }, [src, maxRetries, retryDelay]);

    if (loading) {
        return <p>Loading...</p>;
    }

    if (error) {
        return <p>{error}</p>;
    }

    return <img src={imageSrc} alt={alt} />;
};


const VideoOutput = () => {
    const [sims, setSims] = useState<{ min_sim: number, max_sim: number }>({ min_sim: 0, max_sim: 0 })

    useEffect(() => {
        let eventSource: EventSource | null = null;
        let reconnectTimeout: any = null;

        const connect = () => {
            eventSource = new EventSource(`http://localhost:8001/stats`);

            eventSource.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    setSims(data)
                    console.log(data)
                } catch (err) {
                    console.error(err);
                }
            };

            eventSource.onerror = () => {
                if (eventSource) {
                    eventSource.close();
                }
            };
        };

        connect();

        return () => {
            if (eventSource) {
                eventSource.close();
            }
            clearTimeout(reconnectTimeout);
        };
    }, []);

    const ref = useRef<HTMLImageElement>(null);
    const checkImage = async () => {
        if (ref.current) {
            try {
                await ref.current.decode();
            } catch {
                let src = ref.current.src;
                ref.current.src = '';
                ref.current.src = src;
            }
        }
    };

    useEffect(
        () => {
            let timer1 = setInterval(() => {
                checkImage();
            }, 5000);

            return () => {
                clearInterval(timer1);
            };
        },
        []
    );


    return (
        <>
            <div className="flex w-full max-w-[1200px] items-center justify-center relative">
                <img
                    ref={ref}
                    onError={(e) => console.log('Image Error: ', e)}
                    src="http://localhost:8001/video"
                    width="100%"
                    className="aspect-[1920/1080]"
                    draggable={false}
                    style={{
                        userSelect: 'none',
                    }}
                ></img>
            </div>
            <div className='flex space-x-4'>
                <span>Min Sim:</span>
                <span>{sims.min_sim.toFixed(2)}</span>
                <span>Max Sim:</span>
                <span>{sims.max_sim.toFixed(2)}</span>
            </div>
        </>
    );
};
export default VideoOutput;